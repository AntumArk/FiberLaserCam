from __future__ import annotations

import math

import ezdxf
from contour_offsets import generate_contour_offset_loops, generate_contour_offset_segments
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize

from app_sessions import UploadSession

DEFAULT_MIN_HATCH_AREA = 0.30
SEGMENT_QUANT_GRID = 1e-3
DEFAULT_MODE = "hatch"


def build_zone_payload_from_dxf_path(dxf_path: str) -> tuple[list[dict], dict[str, list[list[float]]]]:
    doc = ezdxf.readfile(dxf_path)
    polys = collect_entities_as_polygons(doc)

    zones: list[dict] = []
    zone_map: dict[str, list[list[float]]] = {}

    zone_index = 0
    for poly in polys:
        for part in iter_polygons(poly):
            points = poly_to_points(part)
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


def vec2(value) -> tuple[float, float]:
    return float(value[0]), float(value[1])


def ensure_polygon(points: list[tuple[float, float]]) -> Polygon | None:
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 1e-9:
        return None
    return poly


def poly_to_points(poly: Polygon) -> list[list[float]]:
    coords = list(poly.exterior.coords)[:-1]
    return [[float(x), float(y)] for x, y in coords]


def sample_arc(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float, step_deg: float = 4.0) -> list[tuple[float, float]]:
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


def collect_entities_as_polygons(doc: ezdxf.document.Drawing) -> list[Polygon]:
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
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "POLYLINE":
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    linework.append(LineString([pts[i], pts[i + 1]]))
                if entity.is_closed:
                    linework.append(LineString([pts[-1], pts[0]]))
            if entity.is_closed and len(pts) >= 3:
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "LINE":
            start = vec2(entity.dxf.start)
            end = vec2(entity.dxf.end)
            linework.append(LineString([start, end]))

        elif kind == "CIRCLE":
            center = vec2(entity.dxf.center)
            radius = float(entity.dxf.radius)
            circle_poly = Point(center).buffer(radius, resolution=64)
            direct_polys.append(circle_poly)
            linework.append(LineString(list(circle_poly.exterior.coords)))

        elif kind == "ARC":
            center = vec2(entity.dxf.center)
            pts = sample_arc(
                center[0], center[1], float(entity.dxf.radius), float(entity.dxf.start_angle), float(entity.dxf.end_angle)
            )
            for i in range(len(pts) - 1):
                linework.append(LineString([pts[i], pts[i + 1]]))

        elif kind == "SPLINE":
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
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

    poly_from_lines: list[Polygon] = []
    if linework:
        try:
            for poly in polygonize(linework):
                fixed = ensure_polygon(list(poly.exterior.coords)[:-1])
                if fixed is not None:
                    poly_from_lines.append(fixed)
        except Exception:
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


def extract_lines(geom) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        lines: list[LineString] = []
        for g in geom.geoms:
            lines.extend(extract_lines(g))
        return lines
    return []


def normalize_hatch_geom(geom):
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
            n = normalize_hatch_geom(g)
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


def build_zone_polygons(zone_map: dict[str, list[list[float]]]) -> dict[str, Polygon]:
    result: dict[str, Polygon] = {}
    for zone_id, points in zone_map.items():
        poly = ensure_polygon([(p[0], p[1]) for p in points])
        if poly is not None:
            result[str(zone_id)] = poly
    return result


def zone_hatch_geometry(zone_id: str, zone_polys: dict[str, Polygon], laser_radius: float):
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

    return normalize_hatch_geom(carved)


def outer_only_hatch_geometry(zone_polys: dict[str, Polygon], laser_radius: float):
    if not zone_polys:
        return None

    outer_id = max(zone_polys.keys(), key=lambda zid: zone_polys[zid].area)
    geom = zone_polys.get(outer_id)
    if geom is None or geom.is_empty:
        return None

    if laser_radius > 0:
        geom = geom.buffer(-laser_radius)
        if geom.is_empty:
            return None

    return normalize_hatch_geom(geom)


def hatch_segments_for_angle(geom, angle_deg: float, spacing: float) -> list[list[list[float]]]:
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
        for line in extract_lines(clipped):
            rotated_back = affinity.rotate(line, angle_deg, origin=(0, 0), use_radians=False)
            coords = list(rotated_back.coords)
            if len(coords) < 2:
                continue
            p1 = [float(coords[0][0]), float(coords[0][1])]
            p2 = [float(coords[-1][0]), float(coords[-1][1])]
            segments.append([p1, p2])
        y += spacing

    return segments


def hatch_segments(geom, angle_deg: float, spacing: float) -> list[list[list[float]]]:
    if spacing <= 0:
        return []
    return hatch_segments_for_angle(geom, angle_deg, spacing)


def resolve_spacing(use_manual_spacing: bool, spacing_value: float | None, laser_radius: float) -> tuple[float | None, str | None]:
    if use_manual_spacing:
        if spacing_value is None or spacing_value <= 0:
            return None, "Manual spacing must be greater than 0."
        return spacing_value, None

    spacing = laser_radius * 2.0
    if spacing <= 0:
        return None, "Auto spacing requires laser radius > 0, or enable manual spacing."
    return spacing, None


def geom_area(geom) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    if isinstance(geom, Polygon):
        return float(geom.area)
    if isinstance(geom, MultiPolygon):
        return float(sum(p.area for p in geom.geoms))
    if isinstance(geom, GeometryCollection):
        area = 0.0
        for g in geom.geoms:
            area += geom_area(g)
        return float(area)
    return 0.0


def iter_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty and p.area > 1e-9]
    if isinstance(geom, GeometryCollection):
        polys: list[Polygon] = []
        for g in geom.geoms:
            polys.extend(iter_polygons(g))
        return polys
    return []


def passes_min_width(geom, min_width: float) -> bool:
    if geom is None or geom.is_empty:
        return False
    if min_width <= 0:
        return True
    probe = geom.buffer(-(min_width * 0.5))
    return not probe.is_empty


def regional_kernel_filter(geom, min_area: float):
    if geom is None or geom.is_empty:
        return None

    kernel_radius = max(math.sqrt(max(min_area, 0.0)) * 0.5, 0.0)
    opened = geom
    if kernel_radius > 0:
        opened = geom.buffer(-kernel_radius)
        if opened.is_empty:
            return None
        opened = opened.buffer(kernel_radius)
        if opened.is_empty:
            return None

    kept: list[Polygon] = []
    for poly in iter_polygons(opened):
        if poly.area >= min_area:
            kept.append(poly)

    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    merged = kept[0]
    for p in kept[1:]:
        merged = merged.union(p)
    return normalize_hatch_geom(merged)


def segment_length(seg: list[list[float]]) -> float:
    p1, p2 = seg
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return math.hypot(dx, dy)


def sanitize_segments(
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
        if segment_length(normalized) < min_length:
            dropped_tiny += 1
            continue

        key = (a, b) if a <= b else (b, a)
        if key in seen:
            dropped_dupe += 1
            continue
        seen.add(key)
        cleaned.append(normalized)

    return cleaned, dropped_tiny, dropped_dupe


def generate_hatch_for_selection(
    session: UploadSession,
    selected_ids: list,
    angle: float,
    spacing: float,
    laser_radius: float,
    min_area: float,
    outer_zone_only: bool,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = build_zone_polygons(session.zone_map)
    normalized_ids = [str(zid) for zid in selected_ids]

    normalized_ids.sort(key=lambda zid: zone_polys[zid].area if zid in zone_polys else 0.0, reverse=True)

    if outer_zone_only:
        geom = outer_only_hatch_geometry(zone_polys, laser_radius)
        segments: list[list[list[float]]] = []
        if geom is not None and geom_area(geom) >= min_area and passes_min_width(geom, math.sqrt(max(min_area, 0.0))):
            segments = hatch_segments(geom, angle, spacing)

        min_seg_len = max(spacing * 0.75, laser_radius * 2.0, 0.02)
        segments, dropped_tiny, dropped_dupe = sanitize_segments(
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

        geom = zone_hatch_geometry(zone_id, zone_polys, laser_radius)
        if geom is None:
            continue

        geom = regional_kernel_filter(geom, min_area)
        if geom is None:
            filtered_narrow += 1
            continue

        if geom_area(geom) < min_area:
            filtered_small += 1
            continue
        if not passes_min_width(geom, min_width):
            filtered_narrow += 1
            continue

        zone_segments = hatch_segments(geom, angle, spacing)
        if zone_segments:
            segments.extend(zone_segments)
            used_zones += 1

    min_seg_len = max(spacing * 0.75, laser_radius * 2.0, 0.02)
    segments, dropped_tiny, dropped_dupe = sanitize_segments(
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
        "outerOnlyApplied": 0,
    }
    return segments, stats


def generate_contour_offsets_for_selection(
    session: UploadSession,
    selected_ids: list,
    start_offset: float,
    spacing: float,
    repetitions: int,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = build_zone_polygons(session.zone_map)
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

    min_seg_len = max(spacing * 0.02, 1e-6)
    segments, dropped_tiny, dropped_dupe = sanitize_segments(
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


def build_contour_loops_for_selection(
    session: UploadSession,
    selected_ids: list,
    start_offset: float,
    offset_spacing: float,
    offset_count: int,
) -> list[list[tuple[float, float]]]:
    zone_polys = build_zone_polygons(session.zone_map)
    loops: list[list[tuple[float, float]]] = []
    for zid in [str(z) for z in selected_ids]:
        base = zone_polys.get(zid)
        if base is None:
            continue
        loops.extend(generate_contour_offset_loops(base, start_offset, offset_spacing, offset_count))
    return loops
