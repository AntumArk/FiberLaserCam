"""pcbnew-native geometry extraction and contour-offset generation.

This is an additive, alternate geometry source alongside the existing DXF +
custom-Python offsetting pipeline (see ``app_geometry.py`` /
``contour_offsets.py``). It is only used when the ``pcbnew`` Python module is
importable -- e.g. when running as the live GUI plugin inside KiCad, or when
invoked through KiCad's own bundled Python interpreter for CLI/offline use --
and the input is a KiCad board file. When ``pcbnew`` is not importable (e.g.
a bare DXF input, or an environment without KiCad's Python bindings), the
existing DXF-based pipeline remains the default and is unaffected by this
module.

Compared to the DXF path, this path:
  - groups copper features by net code and merges touching same-net shapes
    with ``SHAPE_POLY_SET.Simplify()``, instead of relying on a geometric
    touching-heuristic on separately-exported DXF polygons;
  - offsets with ``SHAPE_POLY_SET.Inflate()`` (KiCad's Clipper-backed polygon
    engine), instead of a custom per-vertex miter join, avoiding
    self-intersecting artifacts on tight concave curves;
  - detects overcut collisions between different nets with
    ``SHAPE_POLY_SET.BooleanIntersection()``, instead of segment-intersection
    tests on polyline approximations.
"""
from __future__ import annotations

from pathlib import Path

try:
    from contour_offsets import is_back_layer, mirror_ring_x, mirror_rings_x, trim_ring_against_rings  # noqa: F401
except ImportError:
    from kicad_plugin.contour_offsets import (  # noqa: F401
        is_back_layer,
        mirror_ring_x,
        mirror_rings_x,
        trim_ring_against_rings,
    )

Ring = list[tuple[float, float]]

# KiCad's own high-precision arc-approximation tolerance (``ARC_HIGH_DEF_MM``
# in KiCad's C++ ``include/base_units.h``), used as the max error when KiCad
# itself flattens arcs/circles into polygon segments (e.g. in
# ``TransformShapeToPolygon()``/``Inflate()`` below). This constant is not
# exposed on the ``pcbnew`` Python module (it is a plain ``constexpr``, not a
# class member or scripting API), so it is duplicated here rather than
# referenced via ``pcbnew.ARC_HIGH_DEF_MM`` (which does not exist and raises
# ``AttributeError``).
_ARC_HIGH_DEF_MM = 0.005


def is_pcbnew_available() -> bool:
    """Return True if the ``pcbnew`` module can be imported in this process.

    ``pcbnew`` is only importable when running inside KiCad's own Python
    environment (the live GUI plugin, or a script launched via KiCad's
    bundled interpreter / a system KiCad install that puts it on
    ``sys.path``). It is never available in a plain host Python.
    """
    try:
        import pcbnew  # noqa: F401
    except Exception:
        return False
    return True


def _pcbnew():
    import pcbnew

    return pcbnew


def load_board(source):
    """Load a board from a path, or pass through an already-loaded BOARD.

    Accepts a ``str``/``Path`` to a ``.kicad_pcb`` file (works standalone,
    without the KiCad GUI open) or an already-loaded ``pcbnew.BOARD``
    instance (e.g. from ``pcbnew.GetBoard()`` inside the live GUI plugin).
    """
    pcbnew = _pcbnew()
    if isinstance(source, (str, Path)):
        return pcbnew.LoadBoard(str(source))
    return source


def resolve_layer_id(board, layer_name: str) -> int:
    """Resolve a KiCad layer name (e.g. ``"F.Cu"``) to its numeric layer id."""
    layer_id = board.GetLayerID(layer_name)
    if layer_id < 0:
        raise ValueError(f"Unknown KiCad layer name: {layer_name!r}")
    return layer_id


def _copper_items_for_layer(board, layer_id: int):
    items = []
    for pad in board.GetPads():
        if pad.GetLayerSet().Contains(layer_id):
            items.append(pad)
    for track in board.GetTracks():
        # GetTracks() includes both straight tracks (PCB_TRACK) and vias.
        if track.GetLayerSet().Contains(layer_id):
            items.append(track)
    for zone in board.Zones():
        if zone.GetLayerSet().Contains(layer_id):
            items.append(zone)
    return items


def _edge_cuts_outer_polygon(pcbnew, board):
    """Build a ``SHAPE_POLY_SET`` containing only the outer boundary/boundaries
    of the board's own Edge.Cuts outline, discarding any enclosed
    holes/cutouts (e.g. mounting-hole circles drawn directly on Edge.Cuts).

    Uses KiCad's own ``BOARD.GetBoardPolygonOutlines()`` -- the same outline
    resolution used internally for 3D rendering and Gerber/STEP export -- so
    arcs/circles on Edge.Cuts are captured accurately instead of being
    re-approximated by hand. Only the single largest-area outline is kept;
    any other outline reported by ``GetBoardPolygonOutlines()`` (e.g.
    mounting-hole circles/rectangles drawn directly on Edge.Cuts, which are
    resolved as their own top-level outlines rather than as holes of the
    board outline) is discarded rather than exposed as an offsettable
    boundary, since only the board's own profile should ever be laser-cut
    for a board-profile cutting pass.
    """
    raw = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(raw)
    outer = pcbnew.SHAPE_POLY_SET()
    if raw.OutlineCount() == 0:
        return outer
    largest_idx = max(
        range(raw.OutlineCount()),
        key=lambda idx: abs(raw.Outline(idx).Area()),
    )
    outer.AddOutline(raw.Outline(largest_idx))
    return outer


def build_net_polygons_for_layer(board, layer_id: int, clearance_iu: int = 0):
    """Group copper features on a layer by net code, merging touching
    same-net shapes into single polygons via ``Simplify()``.

    Edge.Cuts is special-cased: it has no pads/tracks/zones (those are
    copper-layer concepts), so it is resolved through the board's own
    outline geometry instead, keyed under a single synthetic net code
    (``0``) and containing only the outer boundary/boundaries (see
    ``_edge_cuts_outer_polygon``).

    Returns ``dict[net_code, SHAPE_POLY_SET]``.
    """
    pcbnew = _pcbnew()

    if layer_id == pcbnew.Edge_Cuts:
        outer = _edge_cuts_outer_polygon(pcbnew, board)
        if outer.OutlineCount() == 0:
            return {}
        return {0: outer}

    error_iu = pcbnew.FromMM(_ARC_HIGH_DEF_MM)

    by_net: dict[int, list] = {}
    for item in _copper_items_for_layer(board, layer_id):
        by_net.setdefault(item.GetNetCode(), []).append(item)

    net_polys = {}
    for net_code, items in by_net.items():
        poly = pcbnew.SHAPE_POLY_SET()
        for item in items:
            item.TransformShapeToPolygon(poly, layer_id, clearance_iu, error_iu, pcbnew.ERROR_INSIDE)
        poly.Simplify()
        net_polys[net_code] = poly

    return net_polys


def _contour_to_ring(pcbnew, contour) -> Ring:
    """Convert a pcbnew contour to a plain-tuple ring in mm, negating Y.

    pcbnew's internal coordinate system has Y increasing *downward* (screen-
    like), while KiCad's own DXF plotter (used by kicad-cli, the default
    geometry source elsewhere in this app) negates Y on the way out so the
    exported DXF looks identical -- not vertically flipped -- to the PCB
    editor view when opened in any standard (Y-up) DXF viewer. Negating Y
    here makes this pcbnew-native geometry source produce the exact same
    coordinate convention, so it can be mixed with the DXF-based source (or
    with drill-hole geometry, see ``get_drill_holes_from_board``) without a
    vertical mismatch between them.
    """
    return [
        (pcbnew.ToMM(contour.CPoint(i).x), -pcbnew.ToMM(contour.CPoint(i).y))
        for i in range(contour.PointCount())
    ]


def polyset_to_rings(polyset) -> list[Ring]:
    """Convert a ``SHAPE_POLY_SET`` into a flat list of plain-tuple rings in
    millimetres (outer boundaries and holes each become their own ring,
    matching the ring/nesting-depth model used by the existing DXF-based
    pipeline in ``app_geometry.py``)."""
    pcbnew = _pcbnew()
    rings: list[Ring] = []
    for outline_idx in range(polyset.OutlineCount()):
        rings.append(_contour_to_ring(pcbnew, polyset.Outline(outline_idx)))
        for hole_idx in range(polyset.HoleCount(outline_idx)):
            rings.append(_contour_to_ring(pcbnew, polyset.Hole(outline_idx, hole_idx)))
    return rings


def _grown_polygon(pcbnew, poly, distance_mm: float, error_iu: int):
    grown = pcbnew.SHAPE_POLY_SET(poly)
    if distance_mm != 0.0:
        grown.Inflate(pcbnew.FromMM(distance_mm), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, error_iu)
    return grown


def _polysets_intersect(pcbnew, a, b) -> bool:
    if a.OutlineCount() == 0 or b.OutlineCount() == 0:
        return False
    inter = pcbnew.SHAPE_POLY_SET(a)
    inter.BooleanIntersection(b)
    return inter.Area() > 0.0


def _net_nesting_depths(net_polys: dict[int, object]) -> dict[int, int]:
    """For each net polygon, count how many *other* net polygons contain a
    point on its own outer boundary. Mirrors the "nesting depth" concept in
    ``app_geometry.compute_ring_nesting_depths``, used to alternate offset
    direction for holes/islands so inward and outward offsets don't collide."""
    codes = list(net_polys.keys())
    depths = {code: 0 for code in codes}

    for code in codes:
        poly = net_polys[code]
        if poly.OutlineCount() == 0:
            continue
        outline = poly.Outline(0)
        if outline.PointCount() == 0:
            continue
        test_point = outline.CPoint(0)

        for other_code in codes:
            if other_code == code:
                continue
            if net_polys[other_code].Contains(test_point):
                depths[code] += 1

    return depths


def get_drill_holes_from_board(board) -> list[tuple[float, float, float]]:
    """Extract every drilled through-hole from a loaded board using pcbnew's
    own resolved geometry, instead of re-parsing the raw ``.kicad_pcb`` text
    and re-deriving each pad's footprint-relative position/rotation by hand.

    ``pad.GetPosition()`` already returns the pad's absolute board-space
    position with the footprint's own position, rotation, and mirroring
    (back-side footprints) fully applied by KiCad -- so this avoids the
    coordinate-shift bugs that come from re-implementing that transform
    (e.g. for rotated or back-side footprints) in plain Python/regex.

    Vias are included and treated the same as through-hole pads (plain
    drilled round holes at their center position), matching how they are
    physically drilled on the board.

    Y is negated for the same reason as ``_contour_to_ring``: to match
    KiCad's own DXF-export coordinate convention (and the PCB editor's visual
    orientation) instead of pcbnew's raw Y-down internal frame, so drill
    holes line up with F.Cu/B.Cu isolation routing exported anywhere else in
    this app. Drilling itself is done in the same (front, unflipped)
    orientation shown in the PCB editor, so no X-mirroring is applied here
    (unlike back-side copper/mask layers, see ``get_board_mirror_axis_mm``).

    Returns a de-duplicated list of ``(x_mm, y_mm, diameter_mm)`` tuples.
    """
    pcbnew = _pcbnew()
    holes: list[tuple[float, float, float]] = []

    for pad in board.GetPads():
        if not pad.HasHole():
            continue
        diameter = max(pcbnew.ToMM(pad.GetDrillSizeX()), pcbnew.ToMM(pad.GetDrillSizeY()))
        if diameter <= 0:
            continue
        pos = pad.GetPosition()
        holes.append((pcbnew.ToMM(pos.x), -pcbnew.ToMM(pos.y), diameter))

    for track in board.GetTracks():
        if track.Type() != pcbnew.PCB_VIA_T:
            continue
        diameter = pcbnew.ToMM(track.GetDrillValue())
        if diameter <= 0:
            continue
        pos = track.GetPosition()
        holes.append((pcbnew.ToMM(pos.x), -pcbnew.ToMM(pos.y), diameter))

    unique: dict[tuple[float, float, float], tuple[float, float, float]] = {}
    for x, y, d in holes:
        key = (round(x, 4), round(y, 4), round(d, 4))
        unique[key] = (x, y, d)

    return list(unique.values())


def get_board_x_span_mm(board) -> tuple[float, float] | None:
    """Return the (left, right) X extent of the board's own Edge.Cuts outline
    in mm, or None if the board has no edge-cuts geometry.

    X is unaffected by the Y-negation applied elsewhere in this module (it's
    a plain coordinate flip, not a unit conversion), so this is the same X
    frame used by drill holes, pcbnew-native contours, and kicad-cli's DXF
    export alike. Used by ``get_board_mirror_axis_mm`` to mirror back-layer
    geometry left/right across the board's own footprint.
    """
    pcbnew = _pcbnew()
    bbox = board.GetBoardEdgesBoundingBox()
    if bbox.GetWidth() <= 0:
        return None
    return (pcbnew.ToMM(bbox.GetLeft()), pcbnew.ToMM(bbox.GetRight()))


def get_board_mirror_axis_mm(board) -> float | None:
    """Return the X (mm) of the board's own Edge.Cuts bounding-box center, or
    None if the board has no edge-cuts geometry.

    Back-side layers (``B.Cu``, ``B.Mask``, etc. -- see
    ``contour_offsets.is_back_layer``) are mirrored across this axis so that
    physically flipping the board left/right in place (staying within the
    same footprint) lines the exported geometry up with the actual copper.
    Neither kicad-cli's DXF export nor this module's own polygon extraction
    mirror back layers on their own, so callers must apply this explicitly.
    """
    span = get_board_x_span_mm(board)
    if span is None:
        return None
    return (span[0] + span[1]) / 2.0


def get_board_edge_cuts_bbox_mm(board) -> tuple[float, float, float, float] | None:
    """Return the board's own Edge.Cuts bounding box in mm as
    ``(minx, miny, maxx, maxy)``, or None if the board has no edge-cuts
    geometry.

    Same X/Y frame as ``get_board_x_span_mm``/drill holes/pcbnew-native
    contours/kicad-cli's DXF export (Y negated relative to pcbnew's own
    internal coordinates, which increase downward). Used to derive tiny
    corner alignment marks (see ``contour_offsets.corner_alignment_mark_segments``)
    that get added to every exported DXF file so they all share an
    identical bounding box regardless of which layer's geometry each file
    actually contains -- working around fiber-laser controllers that
    compute their own bounding box per loaded file and center/align on it.
    """
    pcbnew = _pcbnew()
    bbox = board.GetBoardEdgesBoundingBox()
    if bbox.GetWidth() <= 0 or bbox.GetHeight() <= 0:
        return None
    minx = pcbnew.ToMM(bbox.GetLeft())
    maxx = pcbnew.ToMM(bbox.GetRight())
    miny = -pcbnew.ToMM(bbox.GetBottom())
    maxy = -pcbnew.ToMM(bbox.GetTop())
    return (minx, miny, maxx, maxy)


def generate_contour_offsets_from_board(
    board,
    layer_name: str,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
    mirror_axis_mm: float | None = None,
) -> list[tuple[Ring, bool]]:
    """Generate isolation-routing contour offset loops for one copper layer
    directly from a loaded pcbnew ``BOARD``, using KiCad's own Clipper-backed
    polygon engine (``Simplify`` + ``Inflate`` + ``BooleanIntersection``)
    instead of the DXF-parsing + custom-offset pipeline in
    ``contour_offsets.py``.

    Distances (``start_offset``, ``spacing``) are in millimetres, matching
    the units used by the rest of the app. Returns a flat list of
    ``(ring, closed)`` pairs, one per generated loop/path -- see
    ``generate_contour_offsets_from_net_polys`` for what ``closed`` means.

    ``mirror_axis_mm``, when given (e.g. from ``get_board_mirror_axis_mm``),
    mirrors every resulting loop's X coordinate across that axis -- pass this
    for back-side layers (see ``contour_offsets.is_back_layer``) so the
    output lines up with the board once physically flipped over.
    """
    layer_id = resolve_layer_id(board, layer_name)
    net_polys = build_net_polygons_for_layer(board, layer_id)
    return generate_contour_offsets_from_net_polys(
        net_polys,
        start_offset,
        spacing,
        repetitions,
        invert_direction=invert_direction,
        auto_alternate_direction=auto_alternate_direction,
        mirror_axis_mm=mirror_axis_mm,
    )


def generate_contour_offsets_from_net_polys(
    net_polys: dict[int, object],
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
    mirror_axis_mm: float | None = None,
) -> list[tuple[Ring, bool]]:
    """Same offsetting/overcut-prevention logic as
    ``generate_contour_offsets_from_board``, but operating on an
    already-built ``dict[net_code, SHAPE_POLY_SET]`` (e.g. reused across
    repeated preview/export calls instead of re-extracting it from the board
    every time, or a subset restricted to only the currently-selected nets).

    When a net's grown region intrudes into another net's grown region at
    the same step (overcutting risk), only the intruding *stretch* of its
    boundary is trimmed out with ``trim_ring_against_rings`` -- growth is not
    stopped for the whole net, so the rest of its boundary (and later steps
    anywhere it still has room) keeps growing normally instead of being
    undercut everywhere just because one small area got close to a neighbor.
    Returns a flat list of ``(ring, closed)`` pairs: ``closed`` is True for a
    ring that needed no trimming (still a full closed loop), and False for a
    locally trimmed open path -- callers must not close it back to its first
    point (see ``contour_offsets.loop_to_segments``'s ``closed`` parameter).

    ``mirror_axis_mm``, when given, mirrors every resulting loop's X
    coordinate across that axis (see ``generate_contour_offsets_from_board``).
    """
    if not net_polys:
        return []

    pcbnew = _pcbnew()
    error_iu = pcbnew.FromMM(_ARC_HIGH_DEF_MM)

    net_codes = list(net_polys.keys())

    if auto_alternate_direction:
        depths = _net_nesting_depths(net_polys)
    else:
        depths = {code: 0 for code in net_codes}

    def signed_distance(net_code: int, step: int) -> float:
        invert = invert_direction != bool(depths.get(net_code, 0) % 2) if auto_alternate_direction else invert_direction
        sign = -1.0 if invert else 1.0
        return sign * (start_offset + spacing * step)

    active = {code: True for code in net_codes}
    all_loops: list[tuple[Ring, bool]] = []

    for step in range(repetitions):
        grown_by_net = {}
        for net_code in net_codes:
            if not active[net_code]:
                continue
            grown = _grown_polygon(pcbnew, net_polys[net_code], signed_distance(net_code, step), error_iu)
            if grown.OutlineCount() == 0:
                active[net_code] = False
                continue
            grown_by_net[net_code] = grown

        codes_this_step = list(grown_by_net.keys())
        neighbor_rings: dict[int, list[Ring]] = {code: [] for code in codes_this_step}
        for a_idx in range(len(codes_this_step)):
            code_a = codes_this_step[a_idx]
            for b_idx in range(a_idx + 1, len(codes_this_step)):
                code_b = codes_this_step[b_idx]
                if _polysets_intersect(pcbnew, grown_by_net[code_a], grown_by_net[code_b]):
                    neighbor_rings[code_a].extend(polyset_to_rings(grown_by_net[code_b]))
                    neighbor_rings[code_b].extend(polyset_to_rings(grown_by_net[code_a]))

        for net_code in codes_this_step:
            others = neighbor_rings[net_code]
            for ring in polyset_to_rings(grown_by_net[net_code]):
                if not others:
                    all_loops.append((ring, True))
                    continue
                pieces, closed = trim_ring_against_rings(ring, others)
                if closed:
                    if pieces:
                        all_loops.append((pieces[0], True))
                else:
                    all_loops.extend((piece, False) for piece in pieces)

    if mirror_axis_mm is not None:
        all_loops = [(mirror_ring_x(ring, mirror_axis_mm), closed) for ring, closed in all_loops]

    return all_loops
