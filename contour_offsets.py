from __future__ import annotations

import math


def _to_ring(geom) -> list[tuple[float, float]]:
    if geom is None:
        return []

    if isinstance(geom, list) and geom and isinstance(geom[0], (list, tuple)):
        points = [(float(p[0]), float(p[1])) for p in geom]
    elif hasattr(geom, "exterior") and hasattr(geom.exterior, "coords"):
        points = [(float(p[0]), float(p[1])) for p in list(geom.exterior.coords)]
    else:
        return []

    if len(points) < 3:
        return []

    deduped: list[tuple[float, float]] = [points[0]]
    for pt in points[1:]:
        lx, ly = deduped[-1]
        if math.hypot(pt[0] - lx, pt[1] - ly) > 1e-9:
            deduped.append(pt)

    if len(deduped) >= 2:
        fx, fy = deduped[0]
        lx, ly = deduped[-1]
        if math.hypot(fx - lx, fy - ly) <= 1e-9:
            deduped = deduped[:-1]

    if len(deduped) < 3:
        return []
    return deduped


def _signed_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += (x1 * y2) - (x2 * y1)
    return 0.5 * area


def _ensure_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) < 3:
        return []
    if abs(_signed_area(points)) <= 1e-9:
        return []
    return points


def _line_intersection(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> tuple[float, float] | None:
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


def _offset_ring(points: list[tuple[float, float]], distance: float) -> list[tuple[float, float]]:
    points = _ensure_ring(points)
    if not points:
        return []
    if abs(distance) <= 1e-12:
        return points

    n = len(points)
    ccw = _signed_area(points) > 0
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    normals: list[tuple[float, float]] = []

    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return []
        ux, uy = dx / length, dy / length
        if ccw:
            nx, ny = uy, -ux
        else:
            nx, ny = -uy, ux
        normals.append((nx, ny))
        q1 = (x1 + (nx * distance), y1 + (ny * distance))
        q2 = (x2 + (nx * distance), y2 + (ny * distance))
        lines.append((q1, q2))

    out: list[tuple[float, float]] = []
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
            inter = (x + (ax * distance), y + (ay * distance))
        out.append((float(inter[0]), float(inter[1])))

    return _ensure_ring(out)


def generate_contour_offset_segments(
    geom,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
) -> list[list[list[float]]]:
    loops = generate_contour_offset_loops(geom, start_offset, spacing, repetitions, invert_direction=invert_direction)
    segments: list[list[list[float]]] = []
    for loop in loops:
        if len(loop) < 2:
            continue
        for idx in range(len(loop)):
            p1 = [float(loop[idx][0]), float(loop[idx][1])]
            p2 = [float(loop[(idx + 1) % len(loop)][0]), float(loop[(idx + 1) % len(loop)][1])]
            segments.append([p1, p2])
    return segments


def generate_contour_offset_loops(
    geom,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
) -> list[list[tuple[float, float]]]:
    ring = _to_ring(geom)
    if not ring:
        return []
    if start_offset < 0 or spacing < 0 or repetitions <= 0:
        return []

    loops: list[list[tuple[float, float]]] = []
    for step in range(repetitions):
        offset_distance = start_offset + (step * spacing)
        signed_distance = -offset_distance if invert_direction else offset_distance
        offset_ring = _offset_ring(ring, signed_distance)
        if not offset_ring:
            break
        loops.append(offset_ring)

    return loops


def is_back_layer(layer_name: str) -> bool:
    """Return True for KiCad "back side" layers (``B.Cu``, ``B.Mask``,
    ``B.SilkS``, etc.) -- anything whose short layer-name prefix is ``B.``.

    Used to decide which exported layers need to be mirrored left/right (see
    ``mirror_rings_x`` / ``mirror_segments_x``) so the physical geometry lines
    up when the board is flipped over to work on its back side, since neither
    KiCad's DXF export (``kicad-cli``) nor the pcbnew-native geometry source
    in this app mirror back layers on their own.
    """
    return layer_name.strip().upper().startswith("B.")


def mirror_ring_x(ring: list[tuple[float, float]], axis_x: float) -> list[tuple[float, float]]:
    """Mirror a single ring's X coordinates across ``axis_x`` (Y unchanged)."""
    return [(2.0 * axis_x - x, y) for x, y in ring]


def mirror_rings_x(
    rings: list[list[tuple[float, float]]], axis_x: float
) -> list[list[tuple[float, float]]]:
    """Mirror a list of rings' X coordinates across ``axis_x``."""
    return [mirror_ring_x(ring, axis_x) for ring in rings]


def mirror_segments_x(segments: list[list[list[float]]], axis_x: float) -> list[list[list[float]]]:
    """Mirror a list of ``[[x1, y1], [x2, y2]]`` line segments' X coordinates
    across ``axis_x`` (used for hatch-line output, which is a flat segment
    list rather than closed rings)."""
    return [[[2.0 * axis_x - p[0], p[1]] for p in seg] for seg in segments]


def loop_to_segments(loop: list[tuple[float, float]]) -> list[list[list[float]]]:
    """Convert a single closed ring into a list of [p1, p2] line segments."""
    segments: list[list[list[float]]] = []
    if len(loop) < 2:
        return segments
    for idx in range(len(loop)):
        p1 = [float(loop[idx][0]), float(loop[idx][1])]
        p2 = [float(loop[(idx + 1) % len(loop)][0]), float(loop[(idx + 1) % len(loop)][1])]
        segments.append([p1, p2])
    return segments


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            x_at_y = (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            if x < x_at_y:
                inside = not inside
        j = i
    return inside


def _segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _ring_bbox(ring: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _bboxes_overlap(
    bbox_a: tuple[float, float, float, float], bbox_b: tuple[float, float, float, float]
) -> bool:
    ax0, ay0, ax1, ay1 = bbox_a
    bx0, by0, bx1, by1 = bbox_b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _candidate_overlap_pairs(
    bboxes: list[tuple[float, float, float, float] | None],
) -> list[tuple[int, int]]:
    """Broad-phase spatial filter: sweep rings along the x-axis so only rings
    whose bounding boxes could plausibly overlap are checked with the expensive
    per-segment `_rings_overlap` test below. This avoids an O(n^2) full-geometry
    scan when many rings (e.g. pads scattered across a large board) are far apart.

    A fixed-size spatial grid was used here previously, with cell size derived
    from the single largest ring extent. That falls apart as soon as any one
    ring is much larger/longer than the rest (e.g. a trace running most of the
    length of the board next to many small pads): the oversized cell swallows
    the whole board into one bucket and degrades to a full O(n^2) scan. The
    sweep below instead tracks which bounding boxes are "active" (their x-range
    contains the current sweep position) and only compares pairs that are
    simultaneously active, so its cost tracks actual spatial proximity
    regardless of how differently sized the rings are.
    """
    present = [(i, b) for i, b in enumerate(bboxes) if b is not None]
    if len(present) < 2:
        return []

    events: list[tuple[float, int, int]] = []
    for i, (minx, _miny, maxx, _maxy) in present:
        events.append((minx, 0, i))  # 0 = start, sorts before end at same x
        events.append((maxx, 1, i))  # 1 = end
    events.sort(key=lambda e: (e[0], e[1]))

    active: dict[int, tuple[float, float, float, float]] = {}
    pairs: set[tuple[int, int]] = set()
    for _, kind, i in events:
        if kind == 0:
            bi = bboxes[i]
            for j, bj in active.items():
                if bi[3] < bj[1] or bj[3] < bi[1]:
                    continue
                pairs.add((i, j) if i < j else (j, i))
            active[i] = bi
        else:
            active.pop(i, None)

    return sorted(pairs)


def _rings_overlap(ring_a: list[tuple[float, float]], ring_b: list[tuple[float, float]]) -> bool:
    """Return True if two closed rings cross each other or one contains the other."""
    if not ring_a or not ring_b:
        return False

    bbox_a = _ring_bbox(ring_a)
    bbox_b = _ring_bbox(ring_b)
    if not _bboxes_overlap(bbox_a, bbox_b):
        return False

    n, m = len(ring_a), len(ring_b)
    for i in range(n):
        a1, a2 = ring_a[i], ring_a[(i + 1) % n]
        for j in range(m):
            b1, b2 = ring_b[j], ring_b[(j + 1) % m]
            if _segments_intersect(a1, a2, b1, b2):
                return True

    return _point_in_ring(ring_a[0], ring_b) or _point_in_ring(ring_b[0], ring_a)


def _rings_touch_or_overlap(
    ring_a: list[tuple[float, float]], ring_b: list[tuple[float, float]], tol: float = 1e-4
) -> bool:
    """Return True if two closed rings touch (share an edge/vertex) or overlap.

    `_rings_overlap` treats a perfectly touching boundary (shared edge, no
    crossing) as *not* overlapping. Copper features belonging to the same net
    (e.g. a pad and the track soldered to it) are exported as separate closed
    rings even though they are physically one contiguous blob of copper, so
    they typically touch exactly rather than cross. Nudging one ring outward
    by a tiny tolerance first turns that touching contact into a detectable
    overlap.
    """
    if _rings_overlap(ring_a, ring_b):
        return True
    nudged = _offset_ring(ring_a, tol) or ring_a
    return _rings_overlap(nudged, ring_b)


def _touching_groups(
    rings: list[list[tuple[float, float]] | None], tol: float = 1e-4
) -> list[int]:
    """Group rings that touch or overlap each other (at their original,
    un-offset position) into connected components, returned as one
    representative index per ring.

    Rings sharing a component are treated as the same physical copper island
    (e.g. a pad and the track connected to it) and must not be flagged as
    "colliding" with each other while growing isolation-routing offsets —
    only genuinely separate islands should stop each other's growth.
    """
    n = len(rings)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    bboxes = [_ring_bbox(r) if r else None for r in rings]
    for i, j in _candidate_overlap_pairs(bboxes):
        if rings[i] and rings[j] and _rings_touch_or_overlap(rings[i], rings[j], tol):
            union(i, j)

    return [find(i) for i in range(n)]


def generate_contour_offset_loops_multi(
    polys: list,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_flags: list[bool] | None = None,
) -> list[list[list[tuple[float, float]]]]:
    """Generate concentric offset loops for multiple polygons simultaneously.

    When two polygons are close together (e.g. two nearby pads), their
    isolation-routing offset loops can start to cross or overlap once the
    offset distance grows large enough relative to the gap between them —
    causing the laser to re-cut the same area on more than one pass
    (overcutting). This mirrors how 3D-printing slicers stop adding
    concentric perimeter shells once they would collide with a neighboring
    feature: once a pair of polygons' loops overlap at a given offset step,
    growth is stopped for both of them from that step onward, while the
    earlier non-overlapping loops already generated are kept.

    Polygons that already touch or overlap at their original (un-offset)
    position (e.g. a pad and the trace soldered to it, both part of the same
    net) are exempted from colliding with each other: they are physically one
    contiguous piece of copper, so their offset loops are expected to overlap
    near the shared seam, and stopping their growth there would wrongly
    delete the isolation routing around the whole feature.
    """
    n_polys = len(polys)
    rings = [_to_ring(p) for p in polys]
    if invert_flags is None:
        invert_flags = [False] * n_polys

    same_island = _touching_groups(rings)

    active = [bool(r) for r in rings]
    result: list[list[list[tuple[float, float]]]] = [[] for _ in range(n_polys)]

    if start_offset < 0 or spacing < 0 or repetitions <= 0:
        return result

    for step in range(repetitions):
        offset_distance = start_offset + (step * spacing)
        step_rings: list[list[tuple[float, float]] | None] = [None] * n_polys
        for i in range(n_polys):
            if not active[i]:
                continue
            signed_distance = -offset_distance if invert_flags[i] else offset_distance
            offset_ring = _offset_ring(rings[i], signed_distance)
            if not offset_ring:
                active[i] = False
                continue
            step_rings[i] = offset_ring

        collided: set[int] = set()
        bboxes: list[tuple[float, float, float, float] | None] = [
            _ring_bbox(r) if r is not None else None for r in step_rings
        ]
        for i, j in _candidate_overlap_pairs(bboxes):
            if same_island[i] == same_island[j]:
                continue
            if _rings_overlap(step_rings[i], step_rings[j]):
                collided.add(i)
                collided.add(j)

        for i in range(n_polys):
            if i in collided:
                active[i] = False
                continue
            if step_rings[i] is not None:
                result[i].append(step_rings[i])

    return result

