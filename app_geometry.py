from __future__ import annotations

import math

import minidxf as ezdxf
from contour_offsets import (
    generate_contour_offset_loops_multi,
    loop_to_segments,
)

from app_sessions import UploadSession

DEFAULT_MIN_HATCH_AREA = 0.30
SEGMENT_QUANT_GRID = 1e-3
DEFAULT_MODE = "hatch"

Point = tuple[float, float]
Ring = list[Point]


def vec2(value) -> Point:
    return float(value[0]), float(value[1])


def _signed_area(points: Ring) -> float:
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += (x1 * y2) - (x2 * y1)
    return 0.5 * area


def polygon_area(points: Ring) -> float:
    return abs(_signed_area(points))


def polygon_bounds(points: Ring) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def polygon_centroid(points: Ring) -> Point:
    a = _signed_area(points)
    if abs(a) <= 1e-12:
        minx, miny, maxx, maxy = polygon_bounds(points)
        return ((minx + maxx) * 0.5, (miny + maxy) * 0.5)

    factor = 1.0 / (6.0 * a)
    cx = 0.0
    cy = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        cross = (x1 * y2) - (x2 * y1)
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx * factor, cy * factor)


def ensure_polygon(points: list[Point]) -> Ring | None:
    ring = [(float(x), float(y)) for x, y in points]
    if len(ring) < 3:
        return None

    # Remove tiny duplicate steps that can appear in exported DXF entities
    # (for example arc sampling that includes both 0 and 360 degrees).
    deduped: Ring = [ring[0]]
    for pt in ring[1:]:
        last = deduped[-1]
        if math.hypot(pt[0] - last[0], pt[1] - last[1]) > 1e-9:
            deduped.append(pt)
    ring = deduped

    if len(ring) >= 2:
        first = ring[0]
        last = ring[-1]
        if math.hypot(first[0] - last[0], first[1] - last[1]) <= 1e-9:
            ring = ring[:-1]

    if len(ring) < 3:
        return None
    if polygon_area(ring) <= 1e-9:
        return None
    return ring


def poly_to_points(poly: Ring) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in poly]


def sample_arc(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float, step_deg: float = 4.0) -> list[Point]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    if end < start:
        end += 2 * math.pi

    sweep = end - start
    steps = max(8, int(abs(math.degrees(sweep)) / step_deg))
    pts: list[Point] = []
    for i in range(steps + 1):
        t = start + sweep * (i / steps)
        pts.append((center_x + radius * math.cos(t), center_y + radius * math.sin(t)))
    return pts


def _quantize_point(p: Point, tol: float = 1e-4) -> tuple[int, int]:
    return (int(round(p[0] / tol)), int(round(p[1] / tol)))


def _polygonize_segments(segments: list[tuple[Point, Point]], tol: float = 1e-4) -> list[Ring]:
    if not segments:
        return []

    key_to_point: dict[tuple[int, int], Point] = {}
    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    adjacency: dict[tuple[int, int], list[tuple[tuple[int, int], int]]] = {}

    for idx, (a, b) in enumerate(segments):
        ka = _quantize_point(a, tol)
        kb = _quantize_point(b, tol)
        if ka == kb:
            continue
        key_to_point.setdefault(ka, a)
        key_to_point.setdefault(kb, b)
        edge_index = len(edges)
        edges.append((ka, kb))
        adjacency.setdefault(ka, []).append((kb, edge_index))
        adjacency.setdefault(kb, []).append((ka, edge_index))

    used: set[int] = set()
    loops: list[Ring] = []

    for edge_idx, (start, end) in enumerate(edges):
        if edge_idx in used:
            continue

        used.add(edge_idx)
        path = [start, end]
        prev = start
        curr = end

        for _ in range(len(edges) + 2):
            if curr == start:
                break

            candidates = adjacency.get(curr, [])
            next_key = None
            next_edge_idx = None
            backup_key = None
            backup_edge_idx = None

            for neigh, eidx in candidates:
                if eidx in used:
                    continue
                if neigh != prev and next_key is None:
                    next_key = neigh
                    next_edge_idx = eidx
                if backup_key is None:
                    backup_key = neigh
                    backup_edge_idx = eidx

            if next_key is None:
                next_key = backup_key
                next_edge_idx = backup_edge_idx

            if next_key is None or next_edge_idx is None:
                break

            used.add(next_edge_idx)
            path.append(next_key)
            prev, curr = curr, next_key

        if len(path) < 4 or path[-1] != path[0]:
            continue

        ring = [key_to_point[k] for k in path[:-1]]
        fixed = ensure_polygon(ring)
        if fixed is not None:
            loops.append(fixed)

    return loops


def collect_entities_as_polygons(doc: ezdxf.Drawing) -> list[Ring]:
    modelspace = doc.modelspace()
    direct_polys: list[Ring] = []
    linework: list[tuple[Point, Point]] = []

    for entity in modelspace:
        kind = entity.dxftype()

        if kind == "LWPOLYLINE":
            pts = [tuple(map(float, pt[:2])) for pt in entity.get_points()]
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    linework.append((pts[i], pts[i + 1]))
                if entity.closed:
                    linework.append((pts[-1], pts[0]))
            if entity.closed and len(pts) >= 3:
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "POLYLINE":
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    linework.append((pts[i], pts[i + 1]))
                if entity.is_closed:
                    linework.append((pts[-1], pts[0]))
            if entity.is_closed and len(pts) >= 3:
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

        elif kind == "LINE":
            linework.append((vec2(entity.dxf.start), vec2(entity.dxf.end)))

        elif kind == "CIRCLE":
            center = vec2(entity.dxf.center)
            radius = float(entity.dxf.radius)
            circle_pts = sample_arc(center[0], center[1], radius, 0.0, 360.0, step_deg=6.0)
            poly = ensure_polygon(circle_pts)
            if poly is not None:
                direct_polys.append(poly)
                for i in range(len(poly)):
                    linework.append((poly[i], poly[(i + 1) % len(poly)]))

        elif kind == "ARC":
            center = vec2(entity.dxf.center)
            pts = sample_arc(
                center[0], center[1], float(entity.dxf.radius), float(entity.dxf.start_angle), float(entity.dxf.end_angle)
            )
            for i in range(len(pts) - 1):
                linework.append((pts[i], pts[i + 1]))

        elif kind == "SPLINE":
            pts3d = list(entity.flattening(0.02))
            pts = [(float(p[0]), float(p[1])) for p in pts3d]
            if len(pts) < 2:
                continue

            for i in range(len(pts) - 1):
                linework.append((pts[i], pts[i + 1]))

            start = pts[0]
            end = pts[-1]
            close_tol = 0.05
            is_closed_like = bool(getattr(entity, "closed", False)) or math.hypot(end[0] - start[0], end[1] - start[1]) <= close_tol
            if is_closed_like:
                linework.append((pts[-1], pts[0]))
                poly = ensure_polygon(pts)
                if poly is not None:
                    direct_polys.append(poly)

    poly_from_lines = _polygonize_segments(linework)
    all_polys = direct_polys + poly_from_lines

    unique: dict[tuple[float, float, float], Ring] = {}
    for poly in all_polys:
        cx, cy = polygon_centroid(poly)
        key = (round(cx, 4), round(cy, 4), round(polygon_area(poly), 4))
        if key not in unique:
            unique[key] = poly

    result = list(unique.values())
    result.sort(key=polygon_area, reverse=True)
    return result


def _is_point_in_ring(point: Point, ring: Ring) -> bool:
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            den = y2 - y1
            if abs(den) <= 1e-12:
                continue
            xin = x1 + ((y - y1) * (x2 - x1) / den)
            if xin > x:
                inside = not inside
    return inside


def _ring_contains_ring(outer: Ring, inner: Ring) -> bool:
    if polygon_area(inner) >= polygon_area(outer):
        return False

    # Bounding-box fast reject: `inner`'s bbox must fit inside `outer`'s bbox
    # before it's even worth doing the expensive per-point containment test
    # below. This is what keeps nesting-depth detection usable on boards with
    # thousands of pads/tracks, where the vast majority of ring pairs are
    # spatially unrelated and would otherwise still pay for a full point-in-
    # polygon scan just to be rejected.
    ominx, ominy, omaxx, omaxy = polygon_bounds(outer)
    iminx, iminy, imaxx, imaxy = polygon_bounds(inner)
    if iminx < ominx or iminy < ominy or imaxx > omaxx or imaxy > omaxy:
        return False

    return all(_is_point_in_ring(p, outer) for p in inner)


def _rotate_point(point: Point, angle_deg: float) -> Point:
    x, y = point
    t = math.radians(angle_deg)
    ct = math.cos(t)
    st = math.sin(t)
    return (x * ct - y * st, x * st + y * ct)


def _rotate_ring(ring: Ring, angle_deg: float) -> Ring:
    return [_rotate_point(p, angle_deg) for p in ring]


def _ring_scanline_intersections(ring: Ring, y: float) -> list[float]:
    xs: list[float] = []
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if abs(y2 - y1) <= 1e-12:
            continue
        if y1 > y2:
            x1, x2 = x2, x1
            y1, y2 = y2, y1
        if y < y1 or y >= y2:
            continue
        t = (y - y1) / (y2 - y1)
        xs.append(x1 + (t * (x2 - x1)))
    xs.sort()
    return xs


def _intervals_from_ring(ring: Ring, y: float) -> list[tuple[float, float]]:
    xs = _ring_scanline_intersections(ring, y)
    out: list[tuple[float, float]] = []
    for i in range(0, len(xs) - 1, 2):
        a = xs[i]
        b = xs[i + 1]
        if b > a:
            out.append((a, b))
    return out


def _subtract_intervals(base: list[tuple[float, float]], cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = list(base)
    for c0, c1 in cuts:
        next_result: list[tuple[float, float]] = []
        for a, b in result:
            if c1 <= a or c0 >= b:
                next_result.append((a, b))
                continue
            if c0 > a:
                next_result.append((a, c0))
            if c1 < b:
                next_result.append((c1, b))
        result = next_result
    return [(a, b) for a, b in result if (b - a) > 1e-9]


def build_zone_payload_from_dxf_path(dxf_path: str) -> tuple[list[dict], dict[str, list[list[float]]]]:
    doc = ezdxf.readfile(dxf_path)
    polys = collect_entities_as_polygons(doc)

    zones: list[dict] = []
    zone_map: dict[str, list[list[float]]] = {}

    zone_index = 0
    for poly in polys:
        points = poly_to_points(poly)
        if len(points) < 3:
            continue
        zone_index += 1
        zone_id = str(zone_index)
        minx, miny, maxx, maxy = polygon_bounds(poly)
        zones.append(
            {
                "id": zone_id,
                "points": points,
                "area": float(polygon_area(poly)),
                "bbox": [float(minx), float(miny), float(maxx), float(maxy)],
            }
        )
        zone_map[zone_id] = points

    return zones, zone_map


def build_zone_polygons(zone_map: dict[str, list[list[float]]]) -> dict[str, Ring]:
    result: dict[str, Ring] = {}
    for zone_id, points in zone_map.items():
        poly = ensure_polygon([(p[0], p[1]) for p in points])
        if poly is not None:
            result[str(zone_id)] = poly
    return result


def _line_intersection(
    p1: Point,
    p2: Point,
    p3: Point,
    p4: Point,
) -> Point | None:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return (float(px), float(py))


def _offset_ring(points: Ring, distance: float) -> Ring | None:
    points = ensure_polygon(points)
    if points is None:
        return None
    if abs(distance) <= 1e-12:
        return points

    n = len(points)
    ccw = _signed_area(points) > 0
    lines: list[tuple[Point, Point]] = []
    normals: list[Point] = []

    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return None
        ux, uy = dx / length, dy / length
        if ccw:
            nx, ny = uy, -ux
        else:
            nx, ny = -uy, ux
        normals.append((nx, ny))
        q1 = (x1 + nx * distance, y1 + ny * distance)
        q2 = (x2 + nx * distance, y2 + ny * distance)
        lines.append((q1, q2))

    out: Ring = []
    for i in range(n):
        prev_line = lines[(i - 1) % n]
        curr_line = lines[i]
        inter = _line_intersection(prev_line[0], prev_line[1], curr_line[0], curr_line[1])
        if inter is None:
            x, y = points[i]
            n1x, n1y = normals[(i - 1) % n]
            n2x, n2y = normals[i]
            ax = (n1x + n2x) * 0.5
            ay = (n1y + n2y) * 0.5
            norm = math.hypot(ax, ay)
            if norm <= 1e-12:
                ax, ay = normals[i]
            else:
                ax, ay = ax / norm, ay / norm
            inter = (x + ax * distance, y + ay * distance)
        out.append((float(inter[0]), float(inter[1])))

    return ensure_polygon(out)


def _shrink_region(region: dict[str, object], laser_radius: float) -> dict[str, object] | None:
    if laser_radius <= 0:
        return region

    outer = region["outer"]
    holes = region["holes"]
    assert isinstance(outer, list)
    assert isinstance(holes, list)

    shrunk_outer = _offset_ring(outer, -laser_radius)
    if shrunk_outer is None:
        return None

    grown_holes: list[Ring] = []
    for hole in holes:
        grown = _offset_ring(hole, laser_radius)
        if grown is not None:
            grown_holes.append(grown)

    return {"outer": shrunk_outer, "holes": grown_holes}


def zone_hatch_geometry(zone_id: str, zone_polys: dict[str, Ring], laser_radius: float):
    base = zone_polys.get(zone_id)
    if base is None:
        return None

    holes: list[Ring] = []
    for other_id, other in zone_polys.items():
        if other_id == zone_id:
            continue
        if polygon_area(other) >= polygon_area(base):
            continue
        if _ring_contains_ring(base, other):
            holes.append(other)

    region = {"outer": base, "holes": holes}
    return _shrink_region(region, laser_radius)


def outer_only_hatch_geometry(zone_polys: dict[str, Ring], laser_radius: float):
    if not zone_polys:
        return None
    outer_id = max(zone_polys.keys(), key=lambda zid: polygon_area(zone_polys[zid]))
    region = {"outer": zone_polys[outer_id], "holes": []}
    return _shrink_region(region, laser_radius)


def _selected_zone_ids(zone_polys: dict[str, Ring], selected_ids: list) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for zid in selected_ids:
        key = str(zid)
        if key in seen or key not in zone_polys:
            continue
        seen.add(key)
        selected.append(key)
    selected.sort(key=lambda zid: polygon_area(zone_polys[zid]), reverse=True)
    return selected


def _bbox_overlap_pairs(
    bboxes: list[tuple[float, float, float, float]],
) -> list[tuple[int, int]]:
    """Interval-sweep broad phase: return index pairs whose bounding boxes
    overlap, without the O(n^2) cost of comparing every pair directly. Used to
    cut down the candidate pairs checked by the (much more expensive)
    full polygon containment test.
    """
    n = len(bboxes)
    if n < 2:
        return []

    events: list[tuple[float, int, int]] = []
    for i, (minx, _miny, maxx, _maxy) in enumerate(bboxes):
        events.append((minx, 0, i))
        events.append((maxx, 1, i))
    events.sort(key=lambda e: (e[0], e[1]))

    active: dict[int, tuple[float, float, float, float]] = {}
    pairs: list[tuple[int, int]] = []
    for _, kind, i in events:
        if kind == 0:
            bi = bboxes[i]
            for j, bj in active.items():
                if bi[3] < bj[1] or bj[3] < bi[1]:
                    continue
                pairs.append((i, j) if i < j else (j, i))
            active[i] = bi
        else:
            active.pop(i, None)
    return pairs


def compute_ring_nesting_depths(polys: list[Ring]) -> list[int]:
    """Compute containment nesting depth (0 = outermost) for a flat list of rings.

    A ring's depth is the number of other rings in the list that fully contain it.
    This is used to auto-detect "holes" (odd depth) versus "solid" outlines (even
    depth) so isolation-routing contours can alternate offset direction without
    requiring the user to manually flip each nested contour.

    Containment implies the rings' bounding boxes overlap, so a bbox broad
    phase is used to skip the (much more expensive) full point-in-polygon
    containment test for ring pairs that plainly can't nest -- this keeps the
    function usable on boards with thousands of pads/tracks/vias, where a
    naive full O(n^2) scan (recomputing each ring's area on every comparison)
    can take many seconds on its own.
    """
    n = len(polys)
    depths = [0] * n
    if n < 2:
        return depths

    areas = [polygon_area(p) for p in polys]
    bboxes = [polygon_bounds(p) for p in polys]

    for i, j in _bbox_overlap_pairs(bboxes):
        # Containment can only go from the larger-area ring to the smaller one.
        if areas[i] < areas[j]:
            smaller, larger = i, j
        elif areas[j] < areas[i]:
            smaller, larger = j, i
        else:
            continue

        ominx, ominy, omaxx, omaxy = bboxes[larger]
        iminx, iminy, imaxx, imaxy = bboxes[smaller]
        if iminx < ominx or iminy < ominy or imaxx > omaxx or imaxy > omaxy:
            continue

        if all(_is_point_in_ring(p, polys[larger]) for p in polys[smaller]):
            depths[smaller] += 1

    return depths


def _build_selected_nesting(zone_polys: dict[str, Ring], selected_ids: list[str]) -> tuple[dict[str, str | None], dict[str, int]]:
    parents: dict[str, str | None] = {zid: None for zid in selected_ids}
    sorted_by_area = sorted(selected_ids, key=lambda zid: polygon_area(zone_polys[zid]))

    for zid in sorted_by_area:
        poly = zone_polys[zid]
        parent_id: str | None = None
        parent_area = float("inf")
        for candidate_id in selected_ids:
            if candidate_id == zid:
                continue
            candidate = zone_polys[candidate_id]
            candidate_area = polygon_area(candidate)
            if candidate_area <= polygon_area(poly):
                continue
            if candidate_area >= parent_area:
                continue
            if _ring_contains_ring(candidate, poly):
                parent_id = candidate_id
                parent_area = candidate_area
        parents[zid] = parent_id

    depths: dict[str, int] = {}

    def resolve_depth(zid: str) -> int:
        if zid in depths:
            return depths[zid]
        parent_id = parents[zid]
        if parent_id is None:
            depths[zid] = 0
            return 0
        depth = resolve_depth(parent_id) + 1
        depths[zid] = depth
        return depth

    for zid in selected_ids:
        resolve_depth(zid)

    return parents, depths


def alternating_hatch_geometries(
    selected_ids: list[str],
    zone_polys: dict[str, Ring],
    laser_radius: float,
    invert_alternate_nesting: bool = False,
) -> list[tuple[str, dict[str, object], int]]:
    valid_ids = [zid for zid in selected_ids if zid in zone_polys]
    if not valid_ids:
        return []

    parents, depths = _build_selected_nesting(zone_polys, valid_ids)
    children: dict[str, list[str]] = {zid: [] for zid in valid_ids}
    for zid, parent in parents.items():
        if parent is not None and parent in children:
            children[parent].append(zid)

    # By default, even nesting depths (outer contours) are hatched and odd
    # depths (first-level holes) are skipped. `invert_alternate_nesting` flips
    # this parity so odd depths get hatched instead.
    keep_parity = 1 if invert_alternate_nesting else 0

    geoms: list[tuple[str, dict[str, object], int]] = []
    for zid in valid_ids:
        depth = depths.get(zid, 0)
        if depth % 2 != keep_parity:
            continue

        hole_ids = [cid for cid in children.get(zid, []) if depths.get(cid, -1) == depth + 1]
        region = {
            "outer": zone_polys[zid],
            "holes": [zone_polys[cid] for cid in hole_ids],
        }
        shrunk = _shrink_region(region, laser_radius)
        if shrunk is not None:
            geoms.append((zid, shrunk, depth))

    geoms.sort(key=lambda item: (item[2], -polygon_area(zone_polys[item[0]])))
    return geoms


def hatch_segments_for_angle(geom, angle_deg: float, spacing: float) -> list[list[list[float]]]:
    if geom is None or spacing <= 0:
        return []

    outer = geom.get("outer")
    holes = geom.get("holes", [])
    if not outer:
        return []

    rotated_outer = _rotate_ring(outer, -angle_deg)
    rotated_holes = [_rotate_ring(h, -angle_deg) for h in holes]

    minx, miny, maxx, maxy = polygon_bounds(rotated_outer)
    segments: list[list[list[float]]] = []

    y = miny - spacing
    while y <= maxy + spacing:
        intervals = _intervals_from_ring(rotated_outer, y)
        for hole in rotated_holes:
            intervals = _subtract_intervals(intervals, _intervals_from_ring(hole, y))

        for x0, x1 in intervals:
            p1 = _rotate_point((x0, y), angle_deg)
            p2 = _rotate_point((x1, y), angle_deg)
            segments.append([[float(p1[0]), float(p1[1])], [float(p2[0]), float(p2[1])]])
        y += spacing

    return segments


#: Fixed angle set used by the "multi-angle" overlapping hatch option (issue #8):
#: overlay 0, 45, and 90 degree passes together for more even coverage instead
#: of a single angle.
MULTI_ANGLE_HATCH_ANGLES: tuple[float, ...] = (0.0, 45.0, 90.0)


def hatch_segments(geom, angle_deg: float, spacing: float, multi_angle: bool = False) -> list[list[list[float]]]:
    if spacing <= 0:
        return []
    if multi_angle:
        segments: list[list[list[float]]] = []
        for a in MULTI_ANGLE_HATCH_ANGLES:
            segments.extend(hatch_segments_for_angle(geom, a, spacing))
        return segments
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
    if geom is None:
        return 0.0
    outer = geom.get("outer")
    holes = geom.get("holes", [])
    if not outer:
        return 0.0
    return max(0.0, polygon_area(outer) - sum(polygon_area(h) for h in holes))


def passes_min_width(geom, min_width: float) -> bool:
    if geom is None:
        return False
    if min_width <= 0:
        return True
    outer = geom.get("outer")
    if not outer:
        return False
    minx, miny, maxx, maxy = polygon_bounds(outer)
    return min((maxx - minx), (maxy - miny)) >= min_width


def regional_kernel_filter(geom, min_area: float):
    if geom is None:
        return None
    if geom_area(geom) < min_area:
        return None
    return geom


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
    alternate_nesting_hatch: bool = False,
    invert_alternate_nesting: bool = False,
    multi_angle_hatch: bool = False,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = build_zone_polygons(session.zone_map)
    normalized_ids = _selected_zone_ids(zone_polys, selected_ids)

    if outer_zone_only:
        geom = outer_only_hatch_geometry(zone_polys, laser_radius)
        segments: list[list[list[float]]] = []
        if geom is not None and geom_area(geom) >= min_area and passes_min_width(geom, math.sqrt(max(min_area, 0.0))):
            segments = hatch_segments(geom, angle, spacing, multi_angle=multi_angle_hatch)

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

    if alternate_nesting_hatch:
        filtered_small = 0
        filtered_narrow = 0
        used_zones = 0
        min_width = math.sqrt(max(min_area, 0.0))

        for _zone_id, geom, _depth in alternating_hatch_geometries(
            normalized_ids, zone_polys, laser_radius, invert_alternate_nesting=invert_alternate_nesting
        ):
            if geom_area(geom) < min_area:
                filtered_small += 1
                continue
            if not passes_min_width(geom, min_width):
                filtered_narrow += 1
                continue

            zone_segments = hatch_segments(geom, angle, spacing, multi_angle=multi_angle_hatch)
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
            "filteredCentroid": 0,
            "filteredNarrow": filtered_narrow,
            "droppedTiny": dropped_tiny,
            "droppedDuplicate": dropped_dupe,
            "outerOnlyApplied": 0,
            "alternatingNestingApplied": 1,
        }
        return segments, stats

    _parents, _depths = _build_selected_nesting(zone_polys, normalized_ids)

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
        if _depths.get(zone_id, 0) % 2 != 0:
            continue
        if polygon_area(poly) < min_area:
            filtered_small += 1
            continue

        c = polygon_centroid(poly)
        ckey = (round(float(c[0]), 4), round(float(c[1]), 4))
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

        zone_segments = hatch_segments(geom, angle, spacing, multi_angle=multi_angle_hatch)
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
        "alternatingNestingApplied": 0,
    }
    return segments, stats


def resolve_contour_invert_flags(
    zone_polys: dict[str, Ring],
    zone_ids: list[str],
    invert_offset_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> dict[str, bool]:
    """Resolve the effective offset-direction flag for each zone.

    When `auto_alternate_direction` is enabled (the default), the containment
    nesting depth of each zone among the given `zone_ids` is used to flip the
    offset direction for zones that sit inside another selected zone (i.e.
    "holes"), so isolation-routing contours alternate outward/inward
    automatically instead of requiring every nested contour to be selected
    and flipped by hand. `invert_offset_direction` sets the base direction
    applied to the outermost (even depth) zones.
    """
    valid_ids = [zid for zid in zone_ids if zid in zone_polys]
    if not auto_alternate_direction:
        return {zid: invert_offset_direction for zid in valid_ids}

    polys = [zone_polys[zid] for zid in valid_ids]
    depths = compute_ring_nesting_depths(polys)
    return {
        zid: invert_offset_direction != bool(depth % 2)
        for zid, depth in zip(valid_ids, depths)
    }


def generate_contour_offsets_for_selection(
    session: UploadSession,
    selected_ids: list,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_offset_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> tuple[list[list[list[float]]], dict[str, int]]:
    zone_polys = build_zone_polygons(session.zone_map)
    normalized_ids = [str(zid) for zid in selected_ids]
    normalized_ids.sort(key=lambda zid: polygon_area(zone_polys[zid]) if zid in zone_polys else 0.0, reverse=True)
    invert_flags = resolve_contour_invert_flags(
        zone_polys, normalized_ids, invert_offset_direction, auto_alternate_direction
    )

    valid_ids = [zid for zid in normalized_ids if zid in zone_polys]
    polys = [zone_polys[zid] for zid in valid_ids]
    flags = [invert_flags.get(zid, invert_offset_direction) for zid in valid_ids]
    loops_per_zone = generate_contour_offset_loops_multi(polys, start_offset, spacing, repetitions, flags)

    segments: list[list[list[float]]] = []
    used_zones = 0
    for zone_loops in loops_per_zone:
        if zone_loops:
            used_zones += 1
        for loop in zone_loops:
            segments.extend(loop_to_segments(loop))

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
    invert_offset_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> list[list[tuple[float, float]]]:
    zone_polys = build_zone_polygons(session.zone_map)
    normalized_ids = [str(z) for z in selected_ids]
    invert_flags = resolve_contour_invert_flags(
        zone_polys, normalized_ids, invert_offset_direction, auto_alternate_direction
    )
    valid_ids = [zid for zid in normalized_ids if zid in zone_polys]
    polys = [zone_polys[zid] for zid in valid_ids]
    flags = [invert_flags.get(zid, invert_offset_direction) for zid in valid_ids]
    loops_per_zone = generate_contour_offset_loops_multi(polys, start_offset, offset_spacing, offset_count, flags)
    loops: list[list[tuple[float, float]]] = []
    for zone_loops in loops_per_zone:
        loops.extend(zone_loops)
    return loops
