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


def _segment_intersection_point(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> tuple[float, float, float] | None:
    """Return ``(x, y, t)`` where ``t`` is the intersection's parametric
    position along ``p1``->``p2`` (0..1), or None if the two *bounded*
    segments don't cross. Unlike `_line_intersection`, this only reports a
    hit within both segments' actual extents (with a small tolerance), not
    the infinite-line intersection."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = q1
    x4, y4 = q2
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    den = (dx1 * dy2) - (dy1 * dx2)
    if abs(den) < 1e-12:
        return None
    t = (((x3 - x1) * dy2) - ((y3 - y1) * dx2)) / den
    u = (((x3 - x1) * dy1) - ((y3 - y1) * dx1)) / den
    eps = 1e-9
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return (float(x1 + (t * dx1)), float(y1 + (t * dy1)), float(t))
    return None


def _point_in_any_ring(point: tuple[float, float], rings: list[list[tuple[float, float]]]) -> bool:
    return any(_point_in_ring(point, ring) for ring in rings if ring)


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


DEFAULT_ALIGNMENT_MARGIN_MM = 1.0
DEFAULT_ALIGNMENT_MARK_LENGTH_MM = 1.0


def corner_alignment_mark_segments(
    bbox: tuple[float, float, float, float],
    margin_mm: float = DEFAULT_ALIGNMENT_MARGIN_MM,
    mark_length_mm: float = DEFAULT_ALIGNMENT_MARK_LENGTH_MM,
) -> list[list[list[float]]]:
    """Return tiny "L"-shaped corner tick-mark line segments for the 4
    corners of ``bbox`` (``(minx, miny, maxx, maxy)``, mm) expanded outward
    by ``margin_mm`` on every side.

    Some fiber-laser controllers compute their own bounding box from
    whatever geometry is present in a loaded file and center/align the job
    on that box, rather than trusting the file's own origin/coordinates.
    Because each exported layer (F.Cu, B.Mask, drill holes, Edge.Cuts, ...)
    can contain different geometry -- and therefore a different bounding
    box -- the machine can end up centering each layer's file slightly
    differently, throwing an otherwise-aligned multi-layer job out of
    registration.

    Adding these tiny corner marks to every exported file -- always
    positioned at the same margin beyond the same Edge.Cuts bounding box,
    regardless of which layer's geometry the rest of that file holds --
    forces every exported file to share an identical overall bounding box,
    so the machine's own auto bounding-box/centering behavior lines every
    layer up consistently.

    Each corner gets two short segments meeting exactly at the expanded
    bbox corner and pointing inward along the X and Y axes, forming an "L"
    bracket; the extreme points of the 8 returned segments are exactly the
    4 corners of the margin-expanded bbox.
    """
    minx, miny, maxx, maxy = bbox
    minx -= margin_mm
    miny -= margin_mm
    maxx += margin_mm
    maxy += margin_mm

    segments: list[list[list[float]]] = []
    # (corner_x, corner_y, inward X sign, inward Y sign) for each of the 4 corners.
    for corner_x, corner_y, dx, dy in (
        (minx, miny, 1.0, 1.0),
        (maxx, miny, -1.0, 1.0),
        (maxx, maxy, -1.0, -1.0),
        (minx, maxy, 1.0, -1.0),
    ):
        segments.append([[corner_x, corner_y], [corner_x + dx * mark_length_mm, corner_y]])
        segments.append([[corner_x, corner_y], [corner_x, corner_y + dy * mark_length_mm]])

    return segments


def loop_to_segments(
    loop: list[tuple[float, float]], closed: bool = True
) -> list[list[list[float]]]:
    """Convert a ring/path into a list of [p1, p2] line segments.

    When ``closed`` is True (the default, matching every loop shape prior to
    local overcut-trimming), the wraparound edge from the last point back to
    the first is included. Pass ``closed=False`` for a path that was locally
    trimmed against a neighboring feature (see
    ``trim_ring_against_rings``/``generate_contour_offset_loops_multi``):
    such a path is intentionally open where it was cut, so the wraparound
    edge must be omitted to avoid drawing a spurious chord across the
    trimmed-away gap.
    """
    segments: list[list[list[float]]] = []
    if len(loop) < 2:
        return segments
    last_idx = len(loop) if closed else len(loop) - 1
    for idx in range(last_idx):
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


def trim_ring_against_rings(
    ring: list[tuple[float, float]], others: list[list[tuple[float, float]]]
) -> tuple[list[list[tuple[float, float]]], bool]:
    """Locally trim ``ring``'s boundary curve so it excludes any portion that
    falls inside one of ``others``, instead of discarding the whole ring.

    This is the core of local (as opposed to whole-shape) overcut avoidance:
    when a contour-offset ring intrudes into a neighboring feature's own
    offset ring only over part of its length (e.g. a long trace running
    close by a pad for a short stretch, then away from it), only the
    intruding stretch is cut out -- the rest of the ring, however far away
    from any conflict, is kept intact and can keep growing on later steps.

    Returns ``(pieces, closed)``:
    - If nothing needs trimming, ``pieces`` is ``[ring]`` and ``closed`` is
      True (the ring is unchanged and still a closed loop).
    - If the ring is fully swallowed by ``others``, ``pieces`` is ``[]``.
    - Otherwise ``pieces`` is one or more open polylines tracing the parts of
      ``ring`` that stay outside ``others``, and ``closed`` is False -- the
      caller must not add a closing edge back to the first point (see
      ``loop_to_segments``'s ``closed`` parameter).
    """
    others = [o for o in others if o and len(o) >= 3]
    if not others or len(ring) < 2:
        return ([ring] if ring else []), True

    n = len(ring)
    seq: list[tuple[float, float]] = []
    for i in range(n):
        p1 = ring[i]
        p2 = ring[(i + 1) % n]
        if not seq or math.hypot(p1[0] - seq[-1][0], p1[1] - seq[-1][1]) > 1e-9:
            seq.append(p1)
        cuts: list[tuple[float, tuple[float, float]]] = []
        for other in others:
            m = len(other)
            for j in range(m):
                q1 = other[j]
                q2 = other[(j + 1) % m]
                hit = _segment_intersection_point(p1, p2, q1, q2)
                if hit is not None:
                    cuts.append((hit[2], (hit[0], hit[1])))
        cuts.sort(key=lambda c: c[0])
        for _, pt in cuts:
            if not seq or math.hypot(pt[0] - seq[-1][0], pt[1] - seq[-1][1]) > 1e-9:
                seq.append(pt)

    if len(seq) >= 2 and math.hypot(seq[0][0] - seq[-1][0], seq[0][1] - seq[-1][1]) <= 1e-9:
        seq.pop()

    npts = len(seq)
    if npts < 2:
        return [ring], True

    keep: list[bool] = []
    for k in range(npts):
        a = seq[k]
        b = seq[(k + 1) % npts]
        mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        keep.append(not _point_in_any_ring(mid, others))

    if all(keep):
        return [ring], True
    if not any(keep):
        return [], False

    start = 0
    for k in range(npts):
        if keep[k] and not keep[(k - 1) % npts]:
            start = k
            break

    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    k = start
    for _ in range(npts):
        if keep[k]:
            if not current:
                current.append(seq[k])
            current.append(seq[(k + 1) % npts])
        elif current:
            pieces.append(current)
            current = []
        k = (k + 1) % npts
    if current:
        pieces.append(current)

    return pieces, False


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


def _segment_bbox(
    p1: tuple[float, float], p2: tuple[float, float]
) -> tuple[float, float, float, float]:
    return (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))


def _bipartite_candidate_pairs(
    bboxes_a: list[tuple[float, float, float, float]],
    bboxes_b: list[tuple[float, float, float, float]],
) -> list[tuple[int, int]]:
    events: list[tuple[float, int, int, int]] = []
    for i, (minx, _miny, maxx, _maxy) in enumerate(bboxes_a):
        events.append((minx, 0, 0, i))
        events.append((maxx, 1, 0, i))
    for j, (minx, _miny, maxx, _maxy) in enumerate(bboxes_b):
        events.append((minx, 0, 1, j))
        events.append((maxx, 1, 1, j))
    events.sort(key=lambda e: (e[0], e[1]))

    active_a: dict[int, tuple[float, float, float, float]] = {}
    active_b: dict[int, tuple[float, float, float, float]] = {}
    pairs: list[tuple[int, int]] = []
    for _, kind, list_id, idx in events:
        this_active, other_active = (active_a, active_b) if list_id == 0 else (active_b, active_a)
        if kind == 0:
            bbox = (bboxes_a if list_id == 0 else bboxes_b)[idx]
            for other_idx, other_bbox in other_active.items():
                if bbox[3] < other_bbox[1] or other_bbox[3] < bbox[1]:
                    continue
                pairs.append((idx, other_idx) if list_id == 0 else (other_idx, idx))
            this_active[idx] = bbox
        else:
            this_active.pop(idx, None)

    return pairs


def _rings_overlap(ring_a: list[tuple[float, float]], ring_b: list[tuple[float, float]]) -> bool:
    """Return True if two closed rings cross each other or one contains the other."""
    if not ring_a or not ring_b:
        return False

    bbox_a = _ring_bbox(ring_a)
    bbox_b = _ring_bbox(ring_b)
    if not _bboxes_overlap(bbox_a, bbox_b):
        return False

    n, m = len(ring_a), len(ring_b)
    seg_bboxes_a = [_segment_bbox(ring_a[i], ring_a[(i + 1) % n]) for i in range(n)]
    seg_bboxes_b = [_segment_bbox(ring_b[j], ring_b[(j + 1) % m]) for j in range(m)]
    for i, j in _bipartite_candidate_pairs(seg_bboxes_a, seg_bboxes_b):
        a1, a2 = ring_a[i], ring_a[(i + 1) % n]
        b1, b2 = ring_b[j], ring_b[(j + 1) % m]
        if _segments_intersect(a1, a2, b1, b2):
            return True

    return _point_in_ring(ring_a[0], ring_b) or _point_in_ring(ring_b[0], ring_a)


def _rings_cross_or_share_edge(
    ring_a: list[tuple[float, float]], ring_b: list[tuple[float, float]]
) -> bool:
    """Return True if two closed rings' boundaries actually cross or coincide.

    Unlike `_rings_overlap`, this deliberately does *not* treat pure
    containment (one ring wholly nested inside the other, boundaries never
    touching) as a match -- see `_rings_touch_or_overlap` for why that
    distinction matters.
    """
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
    return False


def _rings_touch_or_overlap(
    ring_a: list[tuple[float, float]], ring_b: list[tuple[float, float]], tol: float = 1e-4
) -> bool:
    """Return True if two closed rings touch (share an edge/vertex) or cross.

    Copper features belonging to the same net (e.g. a pad and the track
    soldered to it) are exported as separate closed rings even though they
    are physically one contiguous blob of copper, so they typically touch
    exactly (shared seam) rather than cross. Nudging one ring outward by a
    tiny tolerance first turns that touching contact into a detectable
    crossing.

    Deliberately does *not* treat pure containment (one ring entirely inside
    the other, with a real gap and no shared boundary) as touching: e.g. a
    filled zone's clearance hole around a different net's pad fully contains
    that pad's own copper ring, but they are not the same physical copper --
    exempting them from overcut-prevention trimming (as `_rings_overlap`'s
    containment case would do here) let the pad's isolation offset keep
    growing straight past the clearance gap into the zone's solid copper
    instead of stopping at it.
    """
    if _rings_cross_or_share_edge(ring_a, ring_b):
        return True
    nudged = _offset_ring(ring_a, tol) or ring_a
    return _rings_cross_or_share_edge(nudged, ring_b)


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
) -> list[list[tuple[list[tuple[float, float]], bool]]]:
    """Generate concentric offset loops for multiple polygons simultaneously.

    When two polygons are close together (e.g. two nearby pads, or a long
    trace running near a pad for part of its length), growing each one's
    isolation-routing offset independently can make their loops cross or
    overlap once the offset distance exceeds the gap between them — causing
    the laser to re-cut the same area on more than one pass (overcutting).

    Rather than stopping a colliding polygon's growth entirely for every
    future step (which would also erase loops far away from the actual
    conflict, undercutting the rest of the shape), each step's offset ring is
    trimmed *locally* with ``trim_ring_against_rings``: only the stretch that
    actually intrudes into a neighboring polygon's same-step offset is cut
    out, while the rest of the ring -- and every later step's ring in areas
    with room to grow -- is kept intact. A ring that survives trimming
    untouched is returned as ``(points, True)`` (still a closed loop); a
    locally trimmed ring becomes one or more open paths, each returned as
    ``(points, False)`` (see ``loop_to_segments``'s ``closed`` parameter).

    Polygons that already touch or overlap at their original (un-offset)
    position (e.g. a pad and the trace soldered to it, both part of the same
    net) are exempted from trimming against each other: they are physically
    one contiguous piece of copper, so their offset loops are expected to
    overlap near the shared seam, and cutting into either there would wrongly
    remove isolation routing around the whole feature.
    """
    n_polys = len(polys)
    rings = [_to_ring(p) for p in polys]
    if invert_flags is None:
        invert_flags = [False] * n_polys

    same_island = _touching_groups(rings)

    active = [bool(r) for r in rings]
    result: list[list[tuple[list[tuple[float, float]], bool]]] = [[] for _ in range(n_polys)]

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

        bboxes: list[tuple[float, float, float, float] | None] = [
            _ring_bbox(r) if r is not None else None for r in step_rings
        ]
        neighbor_rings: list[list[list[tuple[float, float]]]] = [[] for _ in range(n_polys)]
        for i, j in _candidate_overlap_pairs(bboxes):
            if same_island[i] == same_island[j]:
                continue
            if _rings_overlap(step_rings[i], step_rings[j]):
                neighbor_rings[i].append(step_rings[j])
                neighbor_rings[j].append(step_rings[i])

        for i in range(n_polys):
            ring_i = step_rings[i]
            if ring_i is None:
                continue
            others = neighbor_rings[i]
            if not others:
                result[i].append((ring_i, True))
                continue
            pieces, closed = trim_ring_against_rings(ring_i, others)
            if closed:
                if pieces:
                    result[i].append((pieces[0], True))
            else:
                result[i].extend((piece, False) for piece in pieces)

    return result

