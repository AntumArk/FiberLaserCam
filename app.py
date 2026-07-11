from __future__ import annotations

import io
import math
import os
import signal
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from contour_offsets import generate_contour_offset_loops, generate_contour_offset_segments
from flask import Flask, jsonify, render_template, request, send_file
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

EPHEMERAL_MODE = os.environ.get("FIBER_LASER_EPHEMERAL", "0") == "1"
SERVER_TOKEN = os.environ.get("FIBER_LASER_SERVER_TOKEN", "").strip()
_START_TS = time.time()
_LAST_HEARTBEAT_TS = _START_TS
_HAS_HEARTBEAT = False
_DISCONNECT_REQUESTED = False
_SHUTDOWN_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()

STARTUP_IDLE_TIMEOUT_SEC = float(os.environ.get("FIBER_LASER_STARTUP_IDLE_TIMEOUT_SEC", "180"))
HEARTBEAT_IDLE_TIMEOUT_SEC = float(os.environ.get("FIBER_LASER_HEARTBEAT_IDLE_TIMEOUT_SEC", "20"))
SESSION_TTL_SEC = float(os.environ.get("FIBER_LASER_SESSION_TTL_SEC", "1800"))
JANITOR_INTERVAL_SEC = float(os.environ.get("FIBER_LASER_JANITOR_INTERVAL_SEC", "30"))
STALE_TEMP_FILE_TTL_SEC = float(os.environ.get("FIBER_LASER_STALE_TEMP_FILE_TTL_SEC", "3600"))


@dataclass
class UploadSession:
    path: str
    zone_map: dict[str, list[list[float]]]
    zone_payload: list[dict]
    created_ts: float
    last_access_ts: float
    temp_paths: list[str]


SESSIONS: dict[str, UploadSession] = {}
DEFAULT_MIN_HATCH_AREA = 0.30
SEGMENT_QUANT_GRID = 1e-3
DEFAULT_MODE = "hatch"


def _token_ok(token: str | None) -> bool:
    if not SERVER_TOKEN:
        return True
    return (token or "").strip() == SERVER_TOKEN


def _touch_heartbeat() -> None:
    global _LAST_HEARTBEAT_TS, _HAS_HEARTBEAT
    with _SHUTDOWN_LOCK:
        _LAST_HEARTBEAT_TS = time.time()
        _HAS_HEARTBEAT = True


def _request_disconnect() -> None:
    global _DISCONNECT_REQUESTED
    with _SHUTDOWN_LOCK:
        _DISCONNECT_REQUESTED = True


def _safe_unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _create_session_record(
    dxf_path: str,
    zone_map: dict[str, list[list[float]]],
    zones: list[dict],
    temp_paths: list[str] | None = None,
) -> tuple[str, UploadSession]:
    now = time.time()
    upload_id = str(uuid.uuid4())
    session = UploadSession(
        path=dxf_path,
        zone_map=zone_map,
        zone_payload=zones,
        created_ts=now,
        last_access_ts=now,
        temp_paths=list(temp_paths or []),
    )
    return upload_id, session


def _touch_session(upload_id: str) -> None:
    with _SESSION_LOCK:
        session = SESSIONS.get(upload_id)
        if session is not None:
            session.last_access_ts = time.time()


def _cleanup_session(upload_id: str, *, remove: bool = True) -> None:
    with _SESSION_LOCK:
        session = SESSIONS.pop(upload_id, None) if remove else SESSIONS.get(upload_id)
    if session is None:
        return
    for temp_path in session.temp_paths:
        _safe_unlink(temp_path)


def _cleanup_expired_sessions() -> None:
    cutoff = time.time() - SESSION_TTL_SEC
    expired_ids: list[str] = []
    with _SESSION_LOCK:
        for upload_id, session in SESSIONS.items():
            if session.last_access_ts <= cutoff:
                expired_ids.append(upload_id)
    for upload_id in expired_ids:
        _cleanup_session(upload_id, remove=True)


def _cleanup_stale_temp_files() -> None:
    now = time.time()
    candidates: list[Path] = [Path(tempfile.gettempdir()), Path(__file__).resolve().parent / "temp_dxf"]

    for base in candidates:
        if not base.exists() or not base.is_dir():
            continue

        for pattern in ("fiberlaser_upload_*.dxf", "*-fiber-web-*.dxf"):
            for entry in base.glob(pattern):
                try:
                    mtime = entry.stat().st_mtime
                except Exception:
                    continue
                if (now - mtime) > STALE_TEMP_FILE_TTL_SEC:
                    _safe_unlink(str(entry))


def _maintenance_janitor() -> None:
    while True:
        time.sleep(max(1.0, JANITOR_INTERVAL_SEC))
        _cleanup_expired_sessions()
        _cleanup_stale_temp_files()


def _auto_shutdown_watchdog() -> None:
    if not EPHEMERAL_MODE:
        return

    while True:
        time.sleep(1.0)
        with _SHUTDOWN_LOCK:
            now = time.time()
            since_start = now - _START_TS
            since_heartbeat = now - _LAST_HEARTBEAT_TS
            has_heartbeat = _HAS_HEARTBEAT
            disconnect_requested = _DISCONNECT_REQUESTED

        if disconnect_requested:
            os.kill(os.getpid(), signal.SIGTERM)
            return

        if not has_heartbeat and since_start > STARTUP_IDLE_TIMEOUT_SEC:
            os.kill(os.getpid(), signal.SIGTERM)
            return

        if has_heartbeat and since_heartbeat > HEARTBEAT_IDLE_TIMEOUT_SEC:
            os.kill(os.getpid(), signal.SIGTERM)
            return


threading.Thread(target=_auto_shutdown_watchdog, daemon=True).start()
threading.Thread(target=_maintenance_janitor, daemon=True).start()


@app.errorhandler(Exception)
def handle_api_exception(exc):
    if request.path.startswith("/api/"):
        if isinstance(exc, HTTPException):
            return jsonify({"error": exc.description}), exc.code
        return jsonify({"error": str(exc) or "Internal server error"}), 500

    if isinstance(exc, HTTPException):
        return exc
    return "Internal server error", 500


def _build_zone_payload_from_dxf_path(dxf_path: str) -> tuple[list[dict], dict[str, list[list[float]]]]:
    doc = ezdxf.readfile(dxf_path)
    polys = _collect_entities_as_polygons(doc)

    zones: list[dict] = []
    zone_map: dict[str, list[list[float]]] = {}

    zone_index = 0
    for poly in polys:
        for part in _iter_polygons(poly):
            points = _poly_to_points(part)
            if len(points) < 3:
                continue
            zone_index += 1
            zone_id = str(zone_index)
            minx, miny, maxx, maxy = part.bounds
            zones.append(
                {
                    "id": zone_id,
                    "points": points,
                    "area": float(part.area),
                    "bbox": [float(minx), float(miny), float(maxx), float(maxy)],
                }
            )
            zone_map[zone_id] = points

    return zones, zone_map


def _create_upload_session_from_dxf_path(dxf_path: str, temp_paths: list[str] | None = None) -> tuple[str, list[dict]]:
    zones, zone_map = _build_zone_payload_from_dxf_path(dxf_path)
    upload_id, session = _create_session_record(dxf_path, zone_map, zones, temp_paths=temp_paths)
    with _SESSION_LOCK:
        SESSIONS[upload_id] = session
    return upload_id, zones


def _vec2(value) -> tuple[float, float]:
    return float(value[0]), float(value[1])


def _ensure_polygon(points: list[tuple[float, float]]) -> Polygon | None:
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.area <= 1e-9:
        return None
    return poly


def _poly_to_points(poly: Polygon) -> list[list[float]]:
    coords = list(poly.exterior.coords)[:-1]
    return [[float(x), float(y)] for x, y in coords]


def _sample_arc(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float, step_deg: float = 4.0) -> list[tuple[float, float]]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    if end < start:
        end += 2 * math.pi

    sweep = end - start
    steps = max(8, int(abs(math.degrees(sweep)) / step_deg))
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = start + sweep * (i / steps)
        pts.append((center_x + radius * math.cos(t), center_y + radius * math.sin(t)))
    return pts


def _collect_entities_as_polygons(doc: ezdxf.document.Drawing) -> list[Polygon]:
    modelspace = doc.modelspace()
    direct_polys: list[Polygon] = []
    linework: list[LineString] = []

    for entity in modelspace:
        kind = entity.dxftype()

        if kind == "LWPOLYLINE":
            pts = [tuple(map(float, pt[:2])) for pt in entity.get_points()]
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    linework.append(LineString([pts[i], pts[i + 1]]))
                if entity.closed:
                    linework.append(LineString([pts[-1], pts[0]]))
            if entity.closed and len(pts) >= 3:
                poly = _ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "POLYLINE":
            pts = [
                (float(v.dxf.location.x), float(v.dxf.location.y))
                for v in entity.vertices
            ]
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    linework.append(LineString([pts[i], pts[i + 1]]))
                if entity.is_closed:
                    linework.append(LineString([pts[-1], pts[0]]))
            if entity.is_closed and len(pts) >= 3:
                poly = _ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "LINE":
            start = _vec2(entity.dxf.start)
            end = _vec2(entity.dxf.end)
            linework.append(LineString([start, end]))

        elif kind == "CIRCLE":
            center = _vec2(entity.dxf.center)
            radius = float(entity.dxf.radius)
            circle_poly = Point(center).buffer(radius, resolution=64)
            direct_polys.append(circle_poly)
            linework.append(LineString(list(circle_poly.exterior.coords)))

        elif kind == "ARC":
            center = _vec2(entity.dxf.center)
            pts = _sample_arc(
                center[0],
                center[1],
                float(entity.dxf.radius),
                float(entity.dxf.start_angle),
                float(entity.dxf.end_angle),
            )
            for i in range(len(pts) - 1):
                linework.append(LineString([pts[i], pts[i + 1]]))

        elif kind == "SPLINE":
            # Flatten SPLINE entities to linework so polygonize can recover
            # closed zones from curve-only DXF files (e.g. cam outputs).
            pts3d = list(entity.flattening(0.02))
            pts = [(float(p[0]), float(p[1])) for p in pts3d]
            if len(pts) < 2:
                continue

            for i in range(len(pts) - 1):
                linework.append(LineString([pts[i], pts[i + 1]]))

            start = pts[0]
            end = pts[-1]
            close_tol = 0.05
            is_closed_like = bool(getattr(entity, "closed", False)) or math.hypot(end[0] - start[0], end[1] - start[1]) <= close_tol
            if is_closed_like:
                linework.append(LineString([pts[-1], pts[0]]))
                poly = _ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

    poly_from_lines: list[Polygon] = []
    if linework:
        try:
            for poly in polygonize(linework):
                fixed = _ensure_polygon(list(poly.exterior.coords)[:-1])
                if fixed is not None:
                    poly_from_lines.append(fixed)
        except Exception:
            # Some DXF files contain mixed/degenerate geometry that can break
            # collection creation in Shapely; keep direct closed entities usable.
            poly_from_lines = []

    all_polys = direct_polys + poly_from_lines

    unique: dict[tuple[float, float, float], Polygon] = {}
    for poly in all_polys:
        c = poly.centroid
        key = (round(c.x, 4), round(c.y, 4), round(poly.area, 4))
        if key not in unique:
            unique[key] = poly

    result = list(unique.values())
    result.sort(key=lambda p: p.area, reverse=True)
    return result


def _extract_lines(geom) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        lines: list[LineString] = []
        for g in geom.geoms:
            lines.extend(_extract_lines(g))
        return lines
    return []


def _normalize_hatch_geom(geom):
    if geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        polys = [p for p in geom.geoms if not p.is_empty and p.area > 1e-9]
        if not polys:
            return None
        if len(polys) == 1:
            return polys[0]
        merged = polys[0]
        for p in polys[1:]:
            merged = merged.union(p)
        if merged.is_empty:
            return None
        if isinstance(merged, (Polygon, MultiPolygon)):
            return merged
        return None
    if isinstance(geom, GeometryCollection):
        polys = []
        for g in geom.geoms:
            n = _normalize_hatch_geom(g)
            if isinstance(n, Polygon):
                polys.append(n)
            elif isinstance(n, MultiPolygon):
                polys.extend([p for p in n.geoms if not p.is_empty and p.area > 1e-9])
        if not polys:
            return None
        if len(polys) == 1:
            return polys[0]
        merged = polys[0]
        for p in polys[1:]:
            merged = merged.union(p)
        if merged.is_empty:
            return None
        if isinstance(merged, (Polygon, MultiPolygon)):
            return merged
        return None
    return None


def _build_zone_polygons(zone_map: dict[str, list[list[float]]]) -> dict[str, Polygon]:
    result: dict[str, Polygon] = {}
    for zone_id, points in zone_map.items():
        poly = _ensure_polygon([(p[0], p[1]) for p in points])
        if poly is not None:
            result[str(zone_id)] = poly
    return result


def _zone_depths(zone_polys: dict[str, Polygon]) -> dict[str, int]:
    depths: dict[str, int] = {}
    for zone_id, poly in zone_polys.items():
        c = poly.representative_point()
        depth = 0
        for other_id, other in zone_polys.items():
            if other_id == zone_id:
                continue
            if other.area <= poly.area:
                continue
            if other.contains(c):
                depth += 1
        depths[zone_id] = depth
    return depths


def _zone_hatch_geometry(zone_id: str, zone_polys: dict[str, Polygon], laser_radius: float):
    base = zone_polys.get(zone_id)
    if base is None:
        return None

    carved = base
    for other_id, other in zone_polys.items():
        if other_id == zone_id:
            continue
        if other.area >= base.area:
            continue
        if not base.covers(other):
            continue
        carved = carved.difference(other)
        if carved.is_empty:
            return None

    if laser_radius > 0:
        carved = carved.buffer(-laser_radius)
        if carved.is_empty:
            return None

    return _normalize_hatch_geom(carved)


def _outer_only_hatch_geometry(zone_polys: dict[str, Polygon], laser_radius: float):
    if not zone_polys:
        return None

    # Outer-only means one cleaning pass across the board envelope:
    # pick the single largest contour and ignore inner contours/holes.
    outer_id = max(zone_polys.keys(), key=lambda zid: zone_polys[zid].area)
    geom = zone_polys.get(outer_id)
    if geom is None or geom.is_empty:
        return None

    if laser_radius > 0:
        geom = geom.buffer(-laser_radius)
        if geom.is_empty:
            return None

    return _normalize_hatch_geom(geom)


def _hatch_segments_for_angle(geom, angle_deg: float, spacing: float) -> list[list[list[float]]]:
    if geom is None or geom.is_empty or spacing <= 0:
        return []

    rotated_poly = affinity.rotate(geom, -angle_deg, origin=(0, 0), use_radians=False)
    minx, miny, maxx, maxy = rotated_poly.bounds
    span = max(maxx - minx, maxy - miny, 1.0)
    xpad = span * 2.0

    segments: list[list[list[float]]] = []
    y = miny - spacing
    while y <= maxy + spacing:
        cut_line = LineString([(minx - xpad, y), (maxx + xpad, y)])
        clipped = rotated_poly.intersection(cut_line)
        for line in _extract_lines(clipped):
            rotated_back = affinity.rotate(line, angle_deg, origin=(0, 0), use_radians=False)
            coords = list(rotated_back.coords)
            if len(coords) < 2:
                continue
            p1 = [float(coords[0][0]), float(coords[0][1])]
            p2 = [float(coords[-1][0]), float(coords[-1][1])]
            segments.append([p1, p2])
        y += spacing

    return segments


def _hatch_segments(geom, angle_deg: float, spacing: float) -> list[list[list[float]]]:
    if spacing <= 0:
        return []
    return _hatch_segments_for_angle(geom, angle_deg, spacing)


def _resolve_spacing(use_manual_spacing: bool, spacing_value: float | None, laser_radius: float) -> tuple[float | None, str | None]:
    if use_manual_spacing:
        if spacing_value is None or spacing_value <= 0:
            return None, "Manual spacing must be greater than 0."
        return spacing_value, None

    spacing = laser_radius * 2.0
    if spacing <= 0:
        return None, "Auto spacing requires laser radius > 0, or enable manual spacing."
    return spacing, None


def _geom_area(geom) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    if isinstance(geom, Polygon):
        return float(geom.area)
    if isinstance(geom, MultiPolygon):
        return float(sum(p.area for p in geom.geoms))
    if isinstance(geom, GeometryCollection):
        area = 0.0
        for g in geom.geoms:
            area += _geom_area(g)
        return float(area)
    return 0.0


def _iter_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty and p.area > 1e-9]
    if isinstance(geom, GeometryCollection):
        polys: list[Polygon] = []
        for g in geom.geoms:
            polys.extend(_iter_polygons(g))
        return polys
    return []


def _passes_min_width(geom, min_width: float) -> bool:
    if geom is None or geom.is_empty:
        return False
    if min_width <= 0:
        return True
    # Keep only geometries that can contain at least this effective width.
    probe = geom.buffer(-(min_width * 0.5))
    return not probe.is_empty


def _regional_kernel_filter(geom, min_area: float):
    if geom is None or geom.is_empty:
        return None

    # Kernel radius derived from requested minimum area; this suppresses
    # narrow local regions even inside large zones (morphological opening).
    kernel_radius = max(math.sqrt(max(min_area, 0.0)) * 0.5, 0.0)
    opened = geom
    if kernel_radius > 0:
        opened = geom.buffer(-kernel_radius)
        if opened.is_empty:
            return None
        opened = opened.buffer(kernel_radius)
        if opened.is_empty:
            return None

    # Keep only connected components that still satisfy minimum area locally.
    kept: list[Polygon] = []
    for poly in _iter_polygons(opened):
        if poly.area >= min_area:
            kept.append(poly)

    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    merged = kept[0]
    for p in kept[1:]:
        merged = merged.union(p)
    return _normalize_hatch_geom(merged)


def _segment_length(seg: list[list[float]]) -> float:
    p1, p2 = seg
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return math.hypot(dx, dy)


def _sanitize_segments(
    segments: list[list[list[float]]],
    min_length: float,
    quant_grid: float,
) -> tuple[list[list[list[float]]], int, int]:
    cleaned: list[list[list[float]]] = []
    dropped_tiny = 0
    dropped_dupe = 0
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()

    q = max(quant_grid, 1e-9)

    for seg in segments:
        p1, p2 = seg
        a = (round(p1[0] / q) * q, round(p1[1] / q) * q)
        b = (round(p2[0] / q) * q, round(p2[1] / q) * q)

        normalized = [[float(a[0]), float(a[1])], [float(b[0]), float(b[1])]]
        if _segment_length(normalized) < min_length:
            dropped_tiny += 1
            continue

        key = (a, b) if a <= b else (b, a)
        if key in seen:
            dropped_dupe += 1
            continue
        seen.add(key)
        cleaned.append(normalized)

    return cleaned, dropped_tiny, dropped_dupe


def _generate_hatch_for_selection(
    session: UploadSession,
    selected_ids: list,
    angle: float,
    spacing: float,
    laser_radius: float,
    min_area: float,
    outer_zone_only: bool,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = _build_zone_polygons(session.zone_map)
    normalized_ids = [str(zid) for zid in selected_ids]

    # Keep larger zones first when centroid-collapsing likely duplicates.
    normalized_ids.sort(key=lambda zid: zone_polys[zid].area if zid in zone_polys else 0.0, reverse=True)

    outer_only_applied = 0
    if outer_zone_only:
        geom = _outer_only_hatch_geometry(zone_polys, laser_radius)
        segments: list[list[list[float]]] = []
        if geom is not None and _geom_area(geom) >= min_area and _passes_min_width(geom, math.sqrt(max(min_area, 0.0))):
            segments = _hatch_segments(geom, angle, spacing)

        min_seg_len = max(spacing * 0.75, laser_radius * 2.0, 0.02)
        segments, dropped_tiny, dropped_dupe = _sanitize_segments(
            segments,
            min_length=min_seg_len,
            quant_grid=SEGMENT_QUANT_GRID,
        )

        stats = {
            "selected": 1 if zone_polys else 0,
            "used": 1 if segments else 0,
            "filteredSmall": 0 if segments else 1,
            "filteredCentroid": 0,
            "filteredNarrow": 0,
            "droppedTiny": dropped_tiny,
            "droppedDuplicate": dropped_dupe,
            "outerOnlyApplied": 1,
        }
        return segments, stats

    segments: list[list[list[float]]] = []
    seen_centroids: set[tuple[float, float]] = set()
    filtered_small = 0
    filtered_centroid = 0
    filtered_narrow = 0
    used_zones = 0
    min_width = math.sqrt(max(min_area, 0.0))

    for zone_id in normalized_ids:
        poly = zone_polys.get(zone_id)
        if poly is None:
            continue
        if poly.area < min_area:
            filtered_small += 1
            continue

        c = poly.centroid
        ckey = (round(float(c.x), 4), round(float(c.y), 4))
        if ckey in seen_centroids:
            filtered_centroid += 1
            continue
        seen_centroids.add(ckey)

        geom = _zone_hatch_geometry(zone_id, zone_polys, laser_radius)
        if geom is None:
            continue

        # Region-level kernel filtering removes dense/narrow islands inside zones.
        geom = _regional_kernel_filter(geom, min_area)
        if geom is None:
            filtered_narrow += 1
            continue

        # Filter on the actual hatch geometry after regional filtering.
        if _geom_area(geom) < min_area:
            filtered_small += 1
            continue
        if not _passes_min_width(geom, min_width):
            filtered_narrow += 1
            continue

        zone_segments = _hatch_segments(geom, angle, spacing)
        if zone_segments:
            segments.extend(zone_segments)
            used_zones += 1

    # Controller-safe segment floor: avoid dense micro-segments that can crash
    # older laser software parsers or motion planners.
    min_seg_len = max(spacing * 0.75, laser_radius * 2.0, 0.02)
    segments, dropped_tiny, dropped_dupe = _sanitize_segments(
        segments,
        min_length=min_seg_len,
        quant_grid=SEGMENT_QUANT_GRID,
    )

    stats = {
        "selected": len(normalized_ids),
        "used": used_zones,
        "filteredSmall": filtered_small,
        "filteredCentroid": filtered_centroid,
        "filteredNarrow": filtered_narrow,
        "droppedTiny": dropped_tiny,
        "droppedDuplicate": dropped_dupe,
        "outerOnlyApplied": outer_only_applied,
    }
    return segments, stats


def _generate_contour_offsets_for_selection(
    session: UploadSession,
    selected_ids: list,
    start_offset: float,
    spacing: float,
    repetitions: int,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = _build_zone_polygons(session.zone_map)
    normalized_ids = [str(zid) for zid in selected_ids]
    normalized_ids.sort(key=lambda zid: zone_polys[zid].area if zid in zone_polys else 0.0, reverse=True)

    segments: list[list[list[float]]] = []
    used_zones = 0
    for zone_id in normalized_ids:
        base = zone_polys.get(zone_id)
        if base is None:
            continue
        zone_segments = generate_contour_offset_segments(base, start_offset, spacing, repetitions)
        if zone_segments:
            segments.extend(zone_segments)
            used_zones += 1

    # Keep contour loops connected; overly aggressive tiny-segment dropping
    # can create visible gaps in offset rings.
    min_seg_len = max(spacing * 0.02, 1e-6)
    segments, dropped_tiny, dropped_dupe = _sanitize_segments(
        segments,
        min_length=min_seg_len,
        quant_grid=1e-5,
    )

    stats = {
        "selected": len(normalized_ids),
        "used": used_zones,
        "filteredSmall": 0,
        "filteredCentroid": 0,
        "filteredNarrow": 0,
        "droppedTiny": dropped_tiny,
        "droppedDuplicate": dropped_dupe,
    }
    return segments, stats


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/ping")
def ping():
    token = request.args.get("token", "")
    if EPHEMERAL_MODE and not _token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    if token:
        _touch_heartbeat()
    return jsonify({"ok": True})


@app.post("/api/heartbeat")
def heartbeat():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "") or request.args.get("token", ""))
    if EPHEMERAL_MODE and not _token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    _touch_heartbeat()
    return jsonify({"ok": True})


@app.post("/api/disconnect")
def disconnect():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "") or request.args.get("token", ""))
    if EPHEMERAL_MODE and not _token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    with _SESSION_LOCK:
        all_ids = list(SESSIONS.keys())
    for sid in all_ids:
        _cleanup_session(sid, remove=True)
    _request_disconnect()
    return jsonify({"ok": True})


@app.post("/api/upload")
def upload_file():
    file = request.files.get("file")
    if file is None or file.filename == "":
        return jsonify({"error": "No file uploaded."}), 400

    _, ext = os.path.splitext(file.filename.lower())
    if ext != ".dxf":
        return jsonify({"error": "Only DXF files are supported."}), 400

    data = file.read()
    if not data:
        return jsonify({"error": "Uploaded file is empty."}), 400

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".dxf", prefix="fiberlaser_upload_")
    temp.write(data)
    temp.flush()
    temp.close()

    try:
        upload_id, zones = _create_upload_session_from_dxf_path(temp.name, temp_paths=[temp.name])
    except Exception as exc:  # noqa: BLE001
        os.unlink(temp.name)
        return jsonify({"error": f"Failed to parse DXF: {exc}"}), 400

    return jsonify({"uploadId": upload_id, "zones": zones})


@app.post("/api/upload-path")
def upload_path():
    payload = request.get_json(silent=True) or {}
    raw_path = str(payload.get("path", "")).strip()
    if not raw_path:
        return jsonify({"error": "DXF path is required."}), 400

    dxf_path = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.isfile(dxf_path):
        return jsonify({"error": f"DXF file not found: {dxf_path}"}), 404

    _, ext = os.path.splitext(dxf_path.lower())
    if ext != ".dxf":
        return jsonify({"error": "Only DXF files are supported."}), 400

    try:
        upload_id, zones = _create_upload_session_from_dxf_path(dxf_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to parse DXF: {exc}"}), 400

    return jsonify({"uploadId": upload_id, "zones": zones})


@app.get("/api/session/<upload_id>")
def get_session(upload_id: str):
    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    _touch_session(upload_id)

    return jsonify({"uploadId": upload_id, "zones": session.zone_payload})


@app.post("/api/preview")
def preview_hatch():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("uploadId")
    selected_ids = payload.get("selectedIds") or []

    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    _touch_session(upload_id)

    mode = str(payload.get("mode", DEFAULT_MODE))
    if mode not in {"hatch", "contour_offsets"}:
        return jsonify({"error": "Invalid mode."}), 400

    if mode == "contour_offsets":
        try:
            start_offset = float(payload.get("offsetStart", 0.2))
            offset_spacing = float(payload.get("offsetSpacing", 0.2))
            offset_count = int(payload.get("offsetCount", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid contour offset values."}), 400

        if start_offset < 0 or offset_spacing < 0 or offset_count <= 0:
            return jsonify({"error": "Contour offset values must be non-negative, with count > 0."}), 400

        segments, stats = _generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
        )
        return jsonify({"segments": segments, "effectiveSpacing": offset_spacing, "stats": stats})

    try:
        angle = float(payload.get("angle", 45))
        laser_radius = float(payload.get("laserRadius", 0.01))
        min_area = float(payload.get("minArea", DEFAULT_MIN_HATCH_AREA))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric control values."}), 400

    if min_area < 0:
        return jsonify({"error": "Minimum area must be >= 0."}), 400

    use_manual_spacing = bool(payload.get("useManualSpacing", False))
    outer_zone_only = bool(payload.get("outerZoneOnly", False))
    spacing_value: float | None = None
    spacing_raw = payload.get("spacing", None)
    if spacing_raw not in (None, ""):
        try:
            spacing_value = float(spacing_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid spacing value."}), 400

    spacing, spacing_error = _resolve_spacing(use_manual_spacing, spacing_value, laser_radius)
    if spacing_error is not None or spacing is None:
        return jsonify({"error": spacing_error}), 400

    segments, stats = _generate_hatch_for_selection(
        session,
        selected_ids,
        angle,
        spacing,
        laser_radius,
        min_area,
        outer_zone_only,
    )

    return jsonify({"segments": segments, "effectiveSpacing": spacing, "stats": stats})


@app.post("/api/export")
def export_dxf():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("uploadId")
    selected_ids = payload.get("selectedIds") or []

    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    _touch_session(upload_id)

    mode = str(payload.get("mode", DEFAULT_MODE))
    if mode not in {"hatch", "contour_offsets"}:
        return jsonify({"error": "Invalid mode."}), 400

    if mode == "contour_offsets":
        try:
            start_offset = float(payload.get("offsetStart", 0.2))
            offset_spacing = float(payload.get("offsetSpacing", 0.2))
            offset_count = int(payload.get("offsetCount", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid contour offset values."}), 400

        if start_offset < 0 or offset_spacing < 0 or offset_count <= 0:
            return jsonify({"error": "Contour offset values must be non-negative, with count > 0."}), 400

        segments, _ = _generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
        )
        zone_polys = _build_zone_polygons(session.zone_map)
        loops: list[list[tuple[float, float]]] = []
        for zid in [str(z) for z in selected_ids]:
            base = zone_polys.get(zid)
            if base is None:
                continue
            loops.extend(generate_contour_offset_loops(base, start_offset, offset_spacing, offset_count))
    else:
        try:
            angle = float(payload.get("angle", 45))
            laser_radius = float(payload.get("laserRadius", 0.01))
            min_area = float(payload.get("minArea", DEFAULT_MIN_HATCH_AREA))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid numeric control values."}), 400

        if min_area < 0:
            return jsonify({"error": "Minimum area must be >= 0."}), 400

        use_manual_spacing = bool(payload.get("useManualSpacing", False))
        outer_zone_only = bool(payload.get("outerZoneOnly", False))
        spacing_value: float | None = None
        spacing_raw = payload.get("spacing", None)
        if spacing_raw not in (None, ""):
            try:
                spacing_value = float(spacing_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid spacing value."}), 400

        spacing, spacing_error = _resolve_spacing(use_manual_spacing, spacing_value, laser_radius)
        if spacing_error is not None or spacing is None:
            return jsonify({"error": spacing_error}), 400

        segments, _ = _generate_hatch_for_selection(
            session,
            selected_ids,
            angle,
            spacing,
            laser_radius,
            min_area,
            outer_zone_only,
        )
        loops = []

    try:
        source_doc = ezdxf.readfile(session.path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to re-open DXF: {exc}"}), 500

    # Export hatch-only geometry so contour entities are not cut in the same pass.
    # Contour-offset mode uses curve entities, so export as R2000+ for support.
    if mode == "contour_offsets":
        source_version = "R2000"
    else:
        source_version = getattr(source_doc, "dxfversion", "R2010") or "R2010"
    doc = ezdxf.new(source_version)
    if "$INSUNITS" in source_doc.header:
        doc.header["$INSUNITS"] = source_doc.header["$INSUNITS"]

    # Inkscape warns that point display style is ignored; remove point-style
    # header vars in exported files to avoid noisy import warnings.
    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in doc.header:
            del doc.header[header_key]

    layer_name = "HATCH_GEN"
    if layer_name not in doc.layers:
        doc.layers.new(layer_name, dxfattribs={"color": 1})

    modelspace = doc.modelspace()
    if mode == "contour_offsets":
        for loop in loops:
            if len(loop) < 3:
                continue
            try:
                modelspace.add_lwpolyline(loop, close=True, dxfattribs={"layer": layer_name})
            except Exception:
                modelspace.add_polyline2d(loop, close=True, dxfattribs={"layer": layer_name})
    else:
        for seg in segments:
            p1, p2 = seg
            modelspace.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

    stream = io.StringIO()
    doc.write(stream)
    out = io.BytesIO(stream.getvalue().encode("utf-8"))
    out.seek(0)

    _cleanup_session(str(upload_id), remove=True)

    return send_file(
        out,
        mimetype="application/dxf",
        as_attachment=True,
        download_name="hatched_output.dxf",
        max_age=0,
    )


if __name__ == "__main__":
    host = os.environ.get("FIBER_LASER_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("FIBER_LASER_WEB_PORT", "5000"))
    debug = os.environ.get("FIBER_LASER_WEB_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)
