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

Ring = list[tuple[float, float]]


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


def build_net_polygons_for_layer(board, layer_id: int, clearance_iu: int = 0):
    """Group copper features on a layer by net code, merging touching
    same-net shapes into single polygons via ``Simplify()``.

    Returns ``dict[net_code, SHAPE_POLY_SET]``.
    """
    pcbnew = _pcbnew()
    error_iu = pcbnew.FromMM(pcbnew.ARC_HIGH_DEF_MM)

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
    return [
        (pcbnew.ToMM(contour.CPoint(i).x), pcbnew.ToMM(contour.CPoint(i).y))
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
        holes.append((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), diameter))

    for track in board.GetTracks():
        if track.Type() != pcbnew.PCB_VIA_T:
            continue
        diameter = pcbnew.ToMM(track.GetDrillValue())
        if diameter <= 0:
            continue
        pos = track.GetPosition()
        holes.append((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y), diameter))

    unique: dict[tuple[float, float, float], tuple[float, float, float]] = {}
    for x, y, d in holes:
        key = (round(x, 4), round(y, 4), round(d, 4))
        unique[key] = (x, y, d)

    return list(unique.values())


def generate_contour_offsets_from_board(
    board,
    layer_name: str,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> list[Ring]:
    """Generate isolation-routing contour offset loops for one copper layer
    directly from a loaded pcbnew ``BOARD``, using KiCad's own Clipper-backed
    polygon engine (``Simplify`` + ``Inflate`` + ``BooleanIntersection``)
    instead of the DXF-parsing + custom-offset pipeline in
    ``contour_offsets.py``.

    Distances (``start_offset``, ``spacing``) are in millimetres, matching
    the units used by the rest of the app. Returns a flat list of closed
    rings (each a list of ``(x, y)`` mm point tuples), one per generated loop,
    growth for a given net stopping as soon as it would collide with another
    net's grown region at the same step (overcut prevention).
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
    )


def generate_contour_offsets_from_net_polys(
    net_polys: dict[int, object],
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> list[Ring]:
    """Same offsetting/overcut-prevention logic as
    ``generate_contour_offsets_from_board``, but operating on an
    already-built ``dict[net_code, SHAPE_POLY_SET]`` (e.g. reused across
    repeated preview/export calls instead of re-extracting it from the board
    every time, or a subset restricted to only the currently-selected nets).
    """
    if not net_polys:
        return []

    pcbnew = _pcbnew()
    error_iu = pcbnew.FromMM(pcbnew.ARC_HIGH_DEF_MM)

    net_codes = list(net_polys.keys())

    if auto_alternate_direction:
        depths = _net_nesting_depths(net_polys)
    else:
        depths = {code: 0 for code in net_codes}

    def signed_distance(net_code: int, step: int) -> float:
        invert = invert_direction != bool(depths.get(net_code, 0) % 2) if auto_alternate_direction else invert_direction
        sign = -1.0 if invert else 1.0
        return sign * (start_offset + spacing * step)

    all_loops: list[Ring] = []

    for net_code in net_codes:
        base_poly = net_polys[net_code]

        for step in range(repetitions):
            grown = _grown_polygon(pcbnew, base_poly, signed_distance(net_code, step), error_iu)
            if grown.OutlineCount() == 0:
                break

            collides = False
            for other_code in net_codes:
                if other_code == net_code:
                    continue
                other_grown = _grown_polygon(
                    pcbnew, net_polys[other_code], signed_distance(other_code, step), error_iu
                )
                if _polysets_intersect(pcbnew, grown, other_grown):
                    collides = True
                    break

            if collides:
                break

            all_loops.extend(polyset_to_rings(grown))

    return all_loops
