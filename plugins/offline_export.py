from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import minidxf as ezdxf
from app_geometry import (
    DEFAULT_MIN_HATCH_AREA,
    build_zone_payload_from_dxf_path,
    collect_entities_as_polygons,
    compute_ring_nesting_depths,
    generate_hatch_for_selection,
)
from app_sessions import UploadSession

try:
    from contour_offsets import (
        corner_alignment_mark_segments,
        generate_contour_offset_loops_multi,
        is_back_layer,
        loop_to_segments,
        mirror_ring_x,
        mirror_segments_x,
    )
except ImportError:
    from kicad_plugin.contour_offsets import (
        corner_alignment_mark_segments,
        generate_contour_offset_loops_multi,
        is_back_layer,
        loop_to_segments,
        mirror_ring_x,
        mirror_segments_x,
    )

try:
    import pcbnew_geometry
except ImportError:
    from kicad_plugin import pcbnew_geometry


def _collect_polygons_from_dxf(doc: ezdxf.Drawing) -> list[list[tuple[float, float]]]:
    return collect_entities_as_polygons(doc)


def _find_kicad_cli() -> str | None:
    for candidate in ("kicad-cli", "kicad-cli.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    # Not on PATH (common on Windows, where KiCad's installer doesn't add its
    # bin/ directory to PATH) -- fall back to locating it next to pcbnew's
    # own install directory, when pcbnew is importable in this process.
    return pcbnew_geometry.find_kicad_cli_near_pcbnew()


def _extract_board_layer_names(board_path: Path) -> list[str]:
    try:
        with board_path.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return ["Edge.Cuts", "F.Cu", "B.Cu"]

    in_layers = False
    layer_names: list[str] = []
    layer_re = re.compile(r"\(\s*\d+\s+\"([^\"]+)\"")

    for line in lines:
        stripped = line.strip()
        if not in_layers and stripped.startswith("(layers"):
            in_layers = True
            continue

        if in_layers and stripped == ")":
            break

        if in_layers:
            m = layer_re.search(line)
            if m:
                layer_names.append(m.group(1))

    return layer_names or ["Edge.Cuts", "F.Cu", "B.Cu"]


def _resolve_board_path(source_path: Path) -> Path:
    suffix = source_path.suffix.lower()
    if suffix == ".kicad_pcb":
        return source_path
    if suffix == ".kicad_pro":
        board_path = source_path.with_suffix(".kicad_pcb")
        if board_path.exists():
            return board_path
        raise RuntimeError(
            f"Could not find board file next to project: expected {board_path}"
        )
    raise RuntimeError(
        "KiCad input must be a .kicad_pcb board or .kicad_pro project file."
    )


_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_AT_RE = re.compile(
    r"\(\s*at\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(?:\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?))?"
)


def _extract_balanced_blocks(text: str, keyword: str) -> list[str]:
    pattern = f"({keyword}"
    blocks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        start = text.find(pattern, i)
        if start < 0:
            break

        after = start + len(pattern)
        if after < n and (text[after].isalnum() or text[after] in "_-."):
            i = after
            continue

        depth = 0
        end = -1
        for j in range(start, n):
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break

        if end < 0:
            break

        blocks.append(text[start : end + 1])
        i = end + 1

    return blocks


def _parse_at(block: str) -> tuple[float, float, float] | None:
    m = _AT_RE.search(block)
    if not m:
        return None
    x = float(m.group(1))
    y = float(m.group(2))
    rot = float(m.group(3)) if m.group(3) is not None else 0.0
    return x, y, rot


def _parse_drill_diameter(block: str) -> float | None:
    drill_blocks = _extract_balanced_blocks(block, "drill")
    if not drill_blocks:
        return None

    drill = drill_blocks[0]
    values = [float(v) for v in _NUMBER_RE.findall(drill)]
    if not values:
        return None

    if "oval" in drill:
        if len(values) >= 2:
            return max(values[0], values[1])
        return values[0]
    return values[0]


def _rotate_point(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    t = math.radians(angle_deg)
    ct = math.cos(t)
    st = math.sin(t)
    return (x * ct - y * st, x * st + y * ct)


def _collect_kicad_drill_holes(board_path: Path) -> list[tuple[float, float, float]]:
    """Collect (x_mm, y_mm, diameter_mm) for every drilled hole on the board
    (through-hole pads and vias, the latter treated as plain round
    through-holes).

    When the ``pcbnew`` module is importable (the live GUI plugin, or the
    AppImage/CLI running through KiCad's own bundled Python), this loads the
    board with pcbnew and reads each hole's already-resolved absolute
    position directly from KiCad's own data model -- see
    ``pcbnew_geometry.get_drill_holes_from_board``. This is the accurate
    path: it can't get footprint rotation/mirroring wrong the way manually
    re-deriving pad positions from the raw text can. Y is negated there to
    match KiCad's own DXF-export coordinate convention (see
    ``pcbnew_geometry._contour_to_ring``); the plain-text fallback below
    negates Y itself for the same reason.

    Falls back to a plain-text regex parse of the ``.kicad_pcb`` file (see
    ``_collect_kicad_drill_holes_from_text``) when ``pcbnew`` isn't
    importable, e.g. a bare host Python without a KiCad install.

    Drilling is done in the same (front, unflipped) orientation shown in the
    PCB editor, so hole X positions are never mirrored here (unlike back-side
    copper/mask layers -- see ``_resolve_mirror_axis_mm``).
    """
    if pcbnew_geometry.is_pcbnew_available():
        board = pcbnew_geometry.load_board(board_path)
        return pcbnew_geometry.get_drill_holes_from_board(board)
    return [(x, -y, d) for x, y, d in _collect_kicad_drill_holes_from_text(board_path)]


def _collect_kicad_drill_holes_from_text(board_path: Path) -> list[tuple[float, float, float]]:
    text = board_path.read_text(encoding="utf-8", errors="replace")
    holes: list[tuple[float, float, float]] = []

    for via_block in _extract_balanced_blocks(text, "via"):
        at = _parse_at(via_block)
        diameter = _parse_drill_diameter(via_block)
        if at is None or diameter is None or diameter <= 0:
            continue
        holes.append((at[0], at[1], diameter))

    for footprint_block in _extract_balanced_blocks(text, "footprint"):
        fp_at = _parse_at(footprint_block)
        if fp_at is None:
            continue
        fpx, fpy, fp_rot = fp_at

        for pad_block in _extract_balanced_blocks(footprint_block, "pad"):
            if not re.search(r"\(\s*pad\b[^()]*\b(thru_hole|np_thru_hole)\b", pad_block):
                continue

            pad_at = _parse_at(pad_block)
            diameter = _parse_drill_diameter(pad_block)
            if pad_at is None or diameter is None or diameter <= 0:
                continue

            local_x, local_y, _ = pad_at
            rx, ry = _rotate_point(local_x, local_y, fp_rot)
            holes.append((fpx + rx, fpy + ry, diameter))

    unique: dict[tuple[float, float, float], tuple[float, float, float]] = {}
    for x, y, d in holes:
        key = (round(x, 4), round(y, 4), round(d, 4))
        unique[key] = (x, y, d)

    return list(unique.values())


def _sample_circle_points(center_x: float, center_y: float, radius: float, segments: int = 72) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(segments):
        t = (2.0 * math.pi * i) / segments
        pts.append((center_x + radius * math.cos(t), center_y + radius * math.sin(t)))
    return pts


def _sample_inward_spiral(
    center_x: float,
    center_y: float,
    outer_radius: float,
    phase_deg: float,
    turns: float,
    inner_ratio: float,
    points_per_turn: int,
) -> list[tuple[float, float]]:
    if turns <= 0:
        return []

    steps = max(24, int(turns * points_per_turn))
    phase = math.radians(phase_deg)
    inner_ratio = min(max(inner_ratio, 0.0), 0.95)
    pts: list[tuple[float, float]] = []

    for i in range(steps + 1):
        s = i / steps
        theta = phase + (2.0 * math.pi * turns * s)
        radius = outer_radius * (1.0 - ((1.0 - inner_ratio) * s))
        pts.append((center_x + radius * math.cos(theta), center_y + radius * math.sin(theta)))

    return pts


def build_kicad_drill_spiral_geometry(
    source_path: Path,
    spiral_turns: float = 1.75,
    spiral_inner_ratio: float = 0.10,
) -> tuple[list[list[tuple[float, float]]], list[list[list[float]]]]:
    board_path = _resolve_board_path(source_path)
    holes = _collect_kicad_drill_holes(board_path)
    if not holes:
        raise RuntimeError("No drill holes were detected in the KiCad board.")

    circles: list[list[tuple[float, float]]] = []
    segments: list[list[list[float]]] = []

    for cx, cy, diameter in holes:
        radius = diameter * 0.5
        if radius <= 0:
            continue

        circles.append(_sample_circle_points(cx, cy, radius, segments=72))

        for phase_deg in (0.0, 120.0, 240.0):
            spiral_pts = _sample_inward_spiral(
                cx,
                cy,
                radius,
                phase_deg,
                turns=spiral_turns,
                inner_ratio=spiral_inner_ratio,
                points_per_turn=84,
            )
            for i in range(len(spiral_pts) - 1):
                p1 = spiral_pts[i]
                p2 = spiral_pts[i + 1]
                segments.append([[float(p1[0]), float(p1[1])], [float(p2[0]), float(p2[1])]])

    return circles, segments


# Default drill "regular contours" style: 4 concentric inward loops, 0.05mm apart,
# starting 0.05mm in from the hole edge. This is now the default drill style; the
# older "spiral" style remains available as an alternative (DRILL_STYLES).
DEFAULT_DRILL_CONTOUR_START_OFFSET = 0.05
DEFAULT_DRILL_CONTOUR_SPACING = 0.05
DEFAULT_DRILL_CONTOUR_COUNT = 4
DRILL_STYLES = ("contour", "spiral")
DEFAULT_DRILL_STYLE = "contour"


def build_kicad_drill_contour_geometry(
    source_path: Path,
    start_offset: float = DEFAULT_DRILL_CONTOUR_START_OFFSET,
    spacing: float = DEFAULT_DRILL_CONTOUR_SPACING,
    repetitions: int = DEFAULT_DRILL_CONTOUR_COUNT,
) -> tuple[list[list[tuple[float, float]]], list[list[list[float]]]]:
    """Generate inward-facing concentric contour loops for each KiCad drill hole.

    Each hole's outer boundary circle is offset inward (toward the hole center)
    `repetitions` times, `spacing` mm apart, starting `start_offset` mm in from
    the edge - similar to the isolation-routing contour_offsets mode, but
    applied per drill hole instead of per copper zone.
    """
    board_path = _resolve_board_path(source_path)
    holes = _collect_kicad_drill_holes(board_path)
    if not holes:
        raise RuntimeError("No drill holes were detected in the KiCad board.")

    circles: list[list[tuple[float, float]]] = []
    rings: list[list[tuple[float, float]]] = []
    for cx, cy, diameter in holes:
        radius = diameter * 0.5
        if radius <= 0:
            continue
        ring = _sample_circle_points(cx, cy, radius, segments=72)
        circles.append(ring)
        rings.append(ring)

    # Generate all holes' contours together (rather than independently) so that
    # closely-spaced holes (e.g. adjacent vias) get the same overlap-prevention
    # behavior as copper zones in generate_contour_offset_loops_multi, instead
    # of potentially overcutting the material between them.
    loops_per_hole = generate_contour_offset_loops_multi(
        rings, start_offset, spacing, repetitions, invert_flags=[True] * len(rings)
    )

    segments: list[list[list[float]]] = []
    for hole_loops in loops_per_hole:
        for points, closed in hole_loops:
            segments.extend(loop_to_segments(points, closed=closed))

    return circles, segments


def build_kicad_drill_geometry(
    source_path: Path,
    style: str = DEFAULT_DRILL_STYLE,
    spiral_turns: float = 1.75,
    spiral_inner_ratio: float = 0.10,
    contour_start_offset: float = DEFAULT_DRILL_CONTOUR_START_OFFSET,
    contour_spacing: float = DEFAULT_DRILL_CONTOUR_SPACING,
    contour_count: int = DEFAULT_DRILL_CONTOUR_COUNT,
) -> tuple[list[list[tuple[float, float]]], list[list[list[float]]]]:
    if style == "spiral":
        return build_kicad_drill_spiral_geometry(
            source_path,
            spiral_turns=spiral_turns,
            spiral_inner_ratio=spiral_inner_ratio,
        )
    if style == "contour":
        return build_kicad_drill_contour_geometry(
            source_path,
            start_offset=contour_start_offset,
            spacing=contour_spacing,
            repetitions=contour_count,
        )
    raise RuntimeError(f"Unknown drill style: {style!r}. Expected one of {DRILL_STYLES}.")


def generate_kicad_drill_dxf(
    source_path: Path,
    output_dxf_path: Path,
    layer_name: str = "DRILL_GEN",
    style: str = DEFAULT_DRILL_STYLE,
    spiral_turns: float = 1.75,
    spiral_inner_ratio: float = 0.10,
    contour_start_offset: float = DEFAULT_DRILL_CONTOUR_START_OFFSET,
    contour_spacing: float = DEFAULT_DRILL_CONTOUR_SPACING,
    contour_count: int = DEFAULT_DRILL_CONTOUR_COUNT,
) -> tuple[int, int]:
    circles, segments = build_kicad_drill_geometry(
        source_path,
        style=style,
        spiral_turns=spiral_turns,
        spiral_inner_ratio=spiral_inner_ratio,
        contour_start_offset=contour_start_offset,
        contour_spacing=contour_spacing,
        contour_count=contour_count,
    )
    if not circles:
        raise RuntimeError("No drill holes were detected in the KiCad board.")

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4

    if layer_name not in doc.layers:
        doc.layers.new(layer_name, dxfattribs={"color": 1})

    msp = doc.modelspace()
    for circle_pts in circles:
        closed_pts = list(circle_pts) + [circle_pts[0]]
        msp.add_lwpolyline(closed_pts, close=False, dxfattribs={"layer": layer_name})

    for seg in segments:
        p1, p2 = seg
        msp.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

    _add_corner_alignment_marks(msp, layer_name, resolve_edge_cuts_bbox_mm(source_path))

    output_dxf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_dxf_path))
    return len(circles), len(segments)


def _export_kicad_to_dxf(source_path: Path, output_dxf_path: Path, layers: str | None) -> None:
    board_path = _resolve_board_path(source_path)
    kicad_cli = _find_kicad_cli()
    if not kicad_cli:
        raise RuntimeError(
            "kicad-cli not found in PATH. Install KiCad CLI or provide a DXF input file."
        )

    layer_set = layers or ",".join(_extract_board_layer_names(board_path))
    command = [
        kicad_cli,
        "pcb",
        "export",
        "dxf",
        str(board_path),
        "-o",
        str(output_dxf_path),
        "--layers",
        layer_set,
        "--mode-single",
        "--output-units",
        "mm",
        "--use-contours",
    ]

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "Unknown export failure."
        raise RuntimeError(f"kicad-cli DXF export failed: {details}")

    if not output_dxf_path.exists():
        raise RuntimeError("kicad-cli finished but no DXF output file was produced.")


_EDGE_CUTS_GRAPHIC_KEYWORDS = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve")
_COORD_PAIR_RE = re.compile(
    r"\(\s*(?:start|end|center|mid|xy)\s+"
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*\)"
)


def _board_edge_cuts_x_center_from_text(board_path: Path) -> float | None:
    """Regex-based fallback for ``pcbnew_geometry.get_board_mirror_axis_mm``,
    used when ``pcbnew`` isn't importable: parse every graphic item on the
    ``Edge.Cuts`` layer directly out of the raw ``.kicad_pcb`` text and
    return the X-center of their combined bounding box."""
    text = board_path.read_text(encoding="utf-8", errors="replace")
    xs: list[float] = []
    for keyword in _EDGE_CUTS_GRAPHIC_KEYWORDS:
        for block in _extract_balanced_blocks(text, keyword):
            if "Edge.Cuts" not in block:
                continue
            for m in _COORD_PAIR_RE.finditer(block):
                xs.append(float(m.group(1)))
    if not xs:
        return None
    return (min(xs) + max(xs)) / 2.0


def _resolve_board_mirror_axis_mm(source_path: Path) -> float | None:
    """Resolve the X (mm) mirror axis for back-side layers from a KiCad
    board/project source: the board's own Edge.Cuts bounding-box center,
    read via ``pcbnew`` when importable, else parsed out of the raw
    ``.kicad_pcb`` text. Returns None if neither is available."""
    try:
        board_path = _resolve_board_path(source_path)
    except RuntimeError:
        return None

    if pcbnew_geometry.is_pcbnew_available():
        board = pcbnew_geometry.load_board(board_path)
        return pcbnew_geometry.get_board_mirror_axis_mm(board)
    return _board_edge_cuts_x_center_from_text(board_path)


def resolve_mirror_axis_mm(source_path: Path, layer_name: str) -> float | None:
    """Resolve the X (mm) axis to mirror ``layer_name``'s geometry across, or
    None if no mirroring is needed/possible.

    Only back-side layers (``B.Cu``, ``B.Mask``, etc. -- see
    ``contour_offsets.is_back_layer``) need mirroring: physically flipping
    the board left/right to work on its back side means their exported
    geometry must be mirrored across the board's own Edge.Cuts bbox center to
    stay aligned with front-side layers and drill holes, since neither
    kicad-cli's DXF export nor this app's pcbnew-native geometry source
    mirror back layers on their own. Front-side layers, Edge.Cuts itself, and
    drill holes are left untouched (drilling is done in the same unflipped
    orientation as F.Cu isolation routing).
    """
    if not is_back_layer(layer_name):
        return None
    return _resolve_board_mirror_axis_mm(source_path)


def _board_edge_cuts_bbox_from_text(board_path: Path) -> tuple[float, float, float, float] | None:
    """Regex-based fallback for ``pcbnew_geometry.get_board_edge_cuts_bbox_mm``,
    used when ``pcbnew`` isn't importable: parse every graphic item on the
    ``Edge.Cuts`` layer directly out of the raw ``.kicad_pcb`` text and
    return their combined bounding box as ``(minx, miny, maxx, maxy)``.

    Y is negated (X unchanged) to match the same DXF-export coordinate
    convention used everywhere else in this module (see
    ``_collect_kicad_drill_holes``).
    """
    text = board_path.read_text(encoding="utf-8", errors="replace")
    xs: list[float] = []
    ys: list[float] = []
    for keyword in _EDGE_CUTS_GRAPHIC_KEYWORDS:
        for block in _extract_balanced_blocks(text, keyword):
            if "Edge.Cuts" not in block:
                continue
            for m in _COORD_PAIR_RE.finditer(block):
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
    if not xs:
        return None
    return (min(xs), -max(ys), max(xs), -min(ys))


def resolve_edge_cuts_bbox_mm(source_path: Path) -> tuple[float, float, float, float] | None:
    """Resolve the board's own Edge.Cuts bounding box (mm) from a KiCad
    board/project source, read via ``pcbnew`` when importable, else parsed
    out of the raw ``.kicad_pcb`` text. Returns None when the source isn't
    (or doesn't reference) a KiCad board -- e.g. a bare DXF input with no
    board reference -- or the board has no Edge.Cuts geometry.

    Used to derive tiny corner alignment marks (see
    ``contour_offsets.corner_alignment_mark_segments``) added to every
    exported DXF file so they all share an identical bounding box
    regardless of which layer's geometry each file actually contains.
    """
    try:
        board_path = _resolve_board_path(source_path)
    except RuntimeError:
        return None

    if pcbnew_geometry.is_pcbnew_available():
        board = pcbnew_geometry.load_board(board_path)
        return pcbnew_geometry.get_board_edge_cuts_bbox_mm(board)
    return _board_edge_cuts_bbox_from_text(board_path)


def _detect_input_kind(source_path: Path, input_format: str) -> str:
    if input_format in ("dxf", "kicad"):
        return input_format

    suffix = source_path.suffix.lower()
    if suffix == ".dxf":
        return "dxf"
    if suffix in (".kicad_pcb", ".kicad_pro"):
        return "kicad"
    raise RuntimeError(
        "Could not auto-detect input type. Use --input-format dxf|kicad."
    )


@contextmanager
def _prepared_input_dxf(source_path: Path, input_format: str, kicad_layers: str | None):
    kind = _detect_input_kind(source_path, input_format)
    if kind == "dxf":
        yield source_path
        return

    with tempfile.TemporaryDirectory(prefix="fiberlasercam-kicad-") as tmp_dir:
        exported = Path(tmp_dir) / f"{source_path.stem}.dxf"
        _export_kicad_to_dxf(source_path, exported, kicad_layers)
        yield exported


def generate_contour_offset_dxf(
    source_dxf_path: Path,
    output_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
    layer_name: str = "F.Cu",
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
    mirror_axis_mm: float | None = None,
    edge_cuts_bbox_mm: tuple[float, float, float, float] | None = None,
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    if auto_alternate_direction and polys:
        depths = compute_ring_nesting_depths(polys)
    else:
        depths = [0] * len(polys)

    invert_flags = [
        (invert_direction != bool(depth % 2)) if auto_alternate_direction else invert_direction
        for depth in depths
    ]
    loops_per_poly = generate_contour_offset_loops_multi(polys, start_offset, spacing, repetitions, invert_flags)
    loops: list[tuple[list[tuple[float, float]], bool]] = [
        piece for poly_loops in loops_per_poly for piece in poly_loops
    ]

    if not loops:
        raise RuntimeError(
            "No contour loops generated from source DXF. "
            "Check selected export layers and contour parameters."
        )

    if mirror_axis_mm is not None:
        loops = [(mirror_ring_x(points, mirror_axis_mm), closed) for points, closed in loops]

    insunits = source_doc.header.get("$INSUNITS") if "$INSUNITS" in source_doc.header else None
    _write_loops_dxf(loops, output_dxf_path, layer_name, insunits=insunits, edge_cuts_bbox_mm=edge_cuts_bbox_mm)
    return len(polys), len(loops)


def _write_loops_dxf(
    loops: list[tuple[list[tuple[float, float]], bool]],
    output_dxf_path: Path,
    layer_name: str,
    insunits: int | None = None,
    edge_cuts_bbox_mm: tuple[float, float, float, float] | None = None,
) -> None:
    out_doc = ezdxf.new("R2000")
    if insunits is not None:
        out_doc.header["$INSUNITS"] = insunits

    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in out_doc.header:
            del out_doc.header[header_key]

    if layer_name not in out_doc.layers:
        out_doc.layers.new(layer_name, dxfattribs={"color": 1})

    msp = out_doc.modelspace()
    for points, closed in loops:
        min_points = 3 if closed else 2
        if len(points) < min_points:
            continue
        try:
            dxf_points = list(points) + [points[0]] if closed else list(points)
            msp.add_lwpolyline(dxf_points, close=False, dxfattribs={"layer": layer_name})
        except Exception:
            continue

    _add_corner_alignment_marks(msp, layer_name, edge_cuts_bbox_mm)

    output_dxf_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(str(output_dxf_path))


def _add_corner_alignment_marks(
    msp, layer_name: str, edge_cuts_bbox_mm: tuple[float, float, float, float] | None
) -> None:
    """Draw tiny corner alignment-mark line segments (see
    ``contour_offsets.corner_alignment_mark_segments``) into ``msp`` on
    ``layer_name`` when ``edge_cuts_bbox_mm`` is available. No-op when it's
    None (e.g. bare DXF input with no board reference)."""
    if edge_cuts_bbox_mm is None:
        return
    for seg in corner_alignment_mark_segments(edge_cuts_bbox_mm):
        p1, p2 = seg
        msp.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})


def generate_contour_offset_dxf_from_board(
    board_source,
    output_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
    layer_name: str = "F.Cu",
    invert_direction: bool = False,
    auto_alternate_direction: bool = True,
) -> tuple[int, int]:
    """pcbnew-native counterpart to ``generate_contour_offset_dxf``.

    Generates isolation-routing contour loops directly from a KiCad board
    (a ``.kicad_pcb``/``.kicad_pro`` path, or an already-loaded
    ``pcbnew.BOARD`` from the live GUI plugin) using KiCad's own
    Clipper-backed polygon engine (see ``pcbnew_geometry.py``) instead of
    exporting to DXF and re-parsing it. Requires ``pcbnew`` to be importable
    (raises ``RuntimeError`` otherwise); callers should fall back to
    ``generate_contour_offset_dxf`` when it is not available.

    Back-side layers (``B.Cu``, ``B.Mask``, etc.) are automatically mirrored
    across the board's own Edge.Cuts bbox center (see
    ``pcbnew_geometry.get_board_mirror_axis_mm``) so the output lines up once
    the board is physically flipped over.
    """
    if not pcbnew_geometry.is_pcbnew_available():
        raise RuntimeError(
            "pcbnew module is not importable in this Python environment; "
            "the pcbnew-native geometry source is unavailable here. "
            "Use the DXF-based export instead, or run via KiCad's own "
            "Python interpreter."
        )

    board = pcbnew_geometry.load_board(board_source)
    layer_id = pcbnew_geometry.resolve_layer_id(board, layer_name)
    net_count = len(pcbnew_geometry.build_net_polygons_for_layer(board, layer_id))

    mirror_axis_mm = (
        pcbnew_geometry.get_board_mirror_axis_mm(board) if is_back_layer(layer_name) else None
    )
    edge_cuts_bbox_mm = pcbnew_geometry.get_board_edge_cuts_bbox_mm(board)

    loops = pcbnew_geometry.generate_contour_offsets_from_board(
        board,
        layer_name,
        start_offset,
        spacing,
        repetitions,
        invert_direction=invert_direction,
        auto_alternate_direction=auto_alternate_direction,
        mirror_axis_mm=mirror_axis_mm,
    )

    if not loops:
        raise RuntimeError(
            "No contour loops generated from board. "
            "Check selected layer and contour parameters."
        )

    _write_loops_dxf(loops, output_dxf_path, layer_name, edge_cuts_bbox_mm=edge_cuts_bbox_mm)
    return net_count, len(loops)


def preview_contour_offset_counts(
    source_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
    auto_alternate_direction: bool = True,
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    if auto_alternate_direction and polys:
        depths = compute_ring_nesting_depths(polys)
    else:
        depths = [0] * len(polys)

    invert_flags = [bool(depth % 2) if auto_alternate_direction else False for depth in depths]
    loops_per_poly = generate_contour_offset_loops_multi(polys, start_offset, spacing, repetitions, invert_flags)
    loops = [piece for poly_loops in loops_per_poly for piece in poly_loops]

    return len(polys), len(loops)


def generate_hatch_dxf(
    source_dxf_path: Path,
    output_dxf_path: Path,
    angle: float,
    spacing: float,
    layer_name: str = "F.Cu",
    laser_radius: float = 0.01,
    min_area: float = DEFAULT_MIN_HATCH_AREA,
    alternate_nesting_hatch: bool = False,
    invert_alternate_nesting: bool = False,
    multi_angle_hatch: bool = False,
    mirror_axis_mm: float | None = None,
    edge_cuts_bbox_mm: tuple[float, float, float, float] | None = None,
) -> tuple[int, int]:
    zones, zone_map = build_zone_payload_from_dxf_path(str(source_dxf_path))
    session = UploadSession(
        path=str(source_dxf_path),
        zone_map=zone_map,
        zone_payload=zones,
        created_ts=0.0,
        last_access_ts=0.0,
        temp_paths=[],
    )
    selected_ids = [zone["id"] for zone in zones]
    segments, _ = generate_hatch_for_selection(
        session,
        selected_ids,
        angle,
        spacing,
        laser_radius,
        min_area,
        False,
        alternate_nesting_hatch,
        invert_alternate_nesting,
        multi_angle_hatch,
    )

    if not segments:
        raise RuntimeError(
            "No hatch segments generated from source DXF. "
            "Check selected export layers and hatch parameters."
        )

    if mirror_axis_mm is not None:
        segments = mirror_segments_x(segments, mirror_axis_mm)

    source_doc = ezdxf.readfile(str(source_dxf_path))
    out_doc = ezdxf.new(getattr(source_doc, "dxfversion", "R2010") or "R2010")
    if "$INSUNITS" in source_doc.header:
        out_doc.header["$INSUNITS"] = source_doc.header["$INSUNITS"]

    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in out_doc.header:
            del out_doc.header[header_key]

    if layer_name not in out_doc.layers:
        out_doc.layers.new(layer_name, dxfattribs={"color": 1})

    msp = out_doc.modelspace()
    for seg in segments:
        p1, p2 = seg
        msp.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

    _add_corner_alignment_marks(msp, layer_name, edge_cuts_bbox_mm)

    output_dxf_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(str(output_dxf_path))
    return len(zones), len(segments)


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="fiberlasercam",
        description="Generate contour-offset loops or hatch fill from a DXF or KiCad board/project file.",
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Path to source input: .dxf, .kicad_pcb, or .kicad_pro.",
    )
    parser.add_argument("output_dxf", type=Path, help="Path to write the generated DXF file.")
    parser.add_argument(
        "--input-format",
        choices=["auto", "dxf", "kicad"],
        default="auto",
        help="Force input parser selection (default: auto by file extension).",
    )
    parser.add_argument(
        "--kicad-layers",
        default=None,
        help="Comma-separated layers for kicad-cli DXF export when source is KiCad (default: all board layers).",
    )
    parser.add_argument(
        "-s", "--start-offset", type=float, default=20.0,
        help="Offset of the first contour loop, in microns (default: 20).",
    )
    parser.add_argument(
        "-i", "--spacing", type=float, default=20.0,
        help="Spacing between successive contour loops, in microns (default: 20).",
    )
    parser.add_argument(
        "-n", "--repetitions", type=int, default=1,
        help="Number of contour loops to generate, contour mode only (default: 1).",
    )
    parser.add_argument(
        "-m", "--mode", choices=["contour", "hatch", "drill"], default="contour",
        help="Generation mode: contour offset loops, angled hatch fill, or KiCad drill spirals (default: contour).",
    )
    parser.add_argument(
        "--angle", type=float, default=45.0,
        help="Hatch line angle in degrees, hatch mode only (default: 45).",
    )
    parser.add_argument(
        "--alternate-nesting",
        action="store_true",
        help="Hatch alternating nested contours (outer hatched, inner skipped, next nested hatched), useful for text islands.",
    )
    parser.add_argument(
        "--invert-alternate-nesting",
        action="store_true",
        help="Invert which nesting parity gets hatched with --alternate-nesting (outer skipped, inner hatched instead).",
    )
    parser.add_argument(
        "--multi-angle",
        action="store_true",
        help="Overlay hatch lines at 0, 45, and 90 degrees together for more even coverage, instead of a single --angle pass.",
    )
    parser.add_argument("--layer-name", default="F.Cu", help="Layer name for generated geometry (default: F.Cu).")
    parser.add_argument(
        "--spiral-turns",
        type=float,
        default=1.75,
        help="Turns per inward drill spiral arm, drill mode with --drill-style spiral only (default: 1.75).",
    )
    parser.add_argument(
        "--spiral-inner-ratio",
        type=float,
        default=0.10,
        help="Spiral end radius as ratio of hole radius, drill mode with --drill-style spiral only (default: 0.10).",
    )
    parser.add_argument(
        "--drill-style",
        choices=list(DRILL_STYLES),
        default=DEFAULT_DRILL_STYLE,
        help="Drill mode geometry style: 'contour' for regular inward concentric loops (default), "
        "or 'spiral' for inward spiral arms.",
    )
    parser.add_argument(
        "--drill-contour-start-offset",
        type=float,
        default=DEFAULT_DRILL_CONTOUR_START_OFFSET,
        help=f"First inward contour offset from hole edge in mm, --drill-style contour only (default: {DEFAULT_DRILL_CONTOUR_START_OFFSET}).",
    )
    parser.add_argument(
        "--drill-contour-spacing",
        type=float,
        default=DEFAULT_DRILL_CONTOUR_SPACING,
        help=f"Spacing between drill contour loops in mm, --drill-style contour only (default: {DEFAULT_DRILL_CONTOUR_SPACING}).",
    )
    parser.add_argument(
        "--drill-contour-count",
        type=int,
        default=DEFAULT_DRILL_CONTOUR_COUNT,
        help=f"Number of inward drill contour loops (perimeters), --drill-style contour only (default: {DEFAULT_DRILL_CONTOUR_COUNT}).",
    )
    parser.add_argument(
        "--invert", action="store_true",
        help="Invert offset direction, contour mode only (offset outward instead of inward).",
    )
    parser.add_argument(
        "--no-auto-alternate", action="store_true",
        help="Disable automatic direction alternation for nested contours (holes), contour mode only. "
        "By default, contours nested inside another contour (holes) automatically use the opposite "
        "offset direction.",
    )
    parser.add_argument(
        "--geometry-source",
        choices=["dxf", "pcbnew"],
        default="dxf",
        help="Geometry backend for contour mode with a KiCad source (default: dxf, using "
        "kicad-cli DXF export). 'pcbnew' uses KiCad's own Python bindings and Clipper-backed "
        "polygon engine directly (no DXF export step); requires running under a Python that "
        "can import 'pcbnew' (KiCad's bundled interpreter, or a system KiCad install).",
    )
    parser.add_argument(
        "--export-all",
        type=Path,
        default=None,
        metavar="CONFIG_JSON",
        help="Export multiple layers to separate DXF files driven by a JSON config file "
        "(see README for the schema). When set, 'source' must be a KiCad board/project file "
        "and 'output_dxf' is treated as the output directory instead of a single file.",
    )
    return parser


def run_export_all(source: Path, config_path: Path, output_dir: Path) -> list[str]:
    """Export multiple layers from a single KiCad board to separate DXF files.

    Each entry of the JSON config's "layers" list describes either a KiCad
    layer to hatch/contour, or the drill holes, and is written out to its own
    DXF file (the machine used by this project cannot combine several layers
    into one file). See README.md for the config schema.
    """
    config = json.loads(config_path.read_text())
    entries = config.get("layers", [])
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Export-all config must contain a non-empty 'layers' list.")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # Resolved once per board (not per layer) so every file in this export-all
    # run shares the exact same corner alignment marks -- see
    # `_add_corner_alignment_marks`/`contour_offsets.corner_alignment_mark_segments`.
    edge_cuts_bbox_mm = resolve_edge_cuts_bbox_mm(source)

    for entry in entries:
        kind = str(entry.get("kind", "layer"))
        output_name = entry.get("output")

        if kind == "drill":
            if not output_name:
                output_name = f"{source.stem}_Drill.dxf"
            output_path = output_dir / output_name
            generate_kicad_drill_dxf(
                source,
                output_path,
                layer_name=str(entry.get("layer_name", "DRILL_GEN")),
                style=str(entry.get("style", DEFAULT_DRILL_STYLE)),
                spiral_turns=float(entry.get("spiral_turns", 1.75)),
                spiral_inner_ratio=float(entry.get("spiral_inner_ratio", 0.10)),
                contour_start_offset=float(entry.get("contour_start_offset", DEFAULT_DRILL_CONTOUR_START_OFFSET)),
                contour_spacing=float(entry.get("contour_spacing", DEFAULT_DRILL_CONTOUR_SPACING)),
                contour_count=int(entry.get("contour_count", DEFAULT_DRILL_CONTOUR_COUNT)),
            )
            written.append(str(output_path))
            continue

        layer = str(entry.get("layer"))
        if not layer:
            raise RuntimeError(f"Export-all layer entry is missing 'layer': {entry}")
        if not output_name:
            output_name = f"{source.stem}_{layer.replace('.', '_').replace('/', '_')}.dxf"
        output_path = output_dir / output_name

        mode = str(entry.get("mode", "contour"))
        geometry_source = str(entry.get("geometry_source", "dxf"))

        if mode == "contour" and geometry_source == "pcbnew":
            board_path = _resolve_board_path(source)
            generate_contour_offset_dxf_from_board(
                board_path,
                output_path,
                float(entry.get("start_offset_mm", 0.02)),
                float(entry.get("spacing_mm", 0.02)),
                int(entry.get("repetitions", 3)),
                layer_name=layer,
                invert_direction=bool(entry.get("invert", False)),
                auto_alternate_direction=bool(entry.get("auto_alternate", True)),
            )
            written.append(str(output_path))
            continue

        with _prepared_input_dxf(source, "kicad", layer) as source_dxf_path:
            mirror_axis_mm = resolve_mirror_axis_mm(source, layer)
            if mode == "hatch":
                generate_hatch_dxf(
                    source_dxf_path,
                    output_path,
                    float(entry.get("angle", 45.0)),
                    float(entry.get("spacing_mm", 0.02)),
                    layer_name=layer,
                    laser_radius=float(entry.get("laser_radius_mm", 0.01)),
                    min_area=float(entry.get("min_area", DEFAULT_MIN_HATCH_AREA)),
                    alternate_nesting_hatch=bool(entry.get("alternate_nesting", False)),
                    invert_alternate_nesting=bool(entry.get("invert_alternate_nesting", False)),
                    multi_angle_hatch=bool(entry.get("multi_angle", False)),
                    edge_cuts_bbox_mm=edge_cuts_bbox_mm,
                    mirror_axis_mm=mirror_axis_mm,
                )
            else:
                generate_contour_offset_dxf(
                    source_dxf_path,
                    output_path,
                    float(entry.get("start_offset_mm", 0.02)),
                    float(entry.get("spacing_mm", 0.02)),
                    int(entry.get("repetitions", 3)),
                    layer_name=layer,
                    invert_direction=bool(entry.get("invert", False)),
                    auto_alternate_direction=bool(entry.get("auto_alternate", True)),
                    mirror_axis_mm=mirror_axis_mm,
                    edge_cuts_bbox_mm=edge_cuts_bbox_mm,
                )
        written.append(str(output_path))

    return written


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    start_offset_mm = args.start_offset / 1000.0
    spacing_mm = args.spacing / 1000.0
    try:
        if args.export_all is not None:
            kind = _detect_input_kind(args.source, args.input_format)
            if kind != "kicad":
                raise RuntimeError("--export-all requires a KiCad source (.kicad_pcb or .kicad_pro).")
            written = run_export_all(args.source, args.export_all, args.output_dxf)
            for path in written:
                print(f"wrote {path}")
            print(f"export-all complete: {len(written)} file(s) -> {args.output_dxf}")
            return 0

        if args.mode == "drill":
            kind = _detect_input_kind(args.source, args.input_format)
            if kind != "kicad":
                raise RuntimeError("Drill mode requires a KiCad source (.kicad_pcb or .kicad_pro).")
            hole_count, segment_count = generate_kicad_drill_dxf(
                args.source,
                args.output_dxf,
                layer_name=args.layer_name,
                style=args.drill_style,
                spiral_turns=args.spiral_turns,
                spiral_inner_ratio=args.spiral_inner_ratio,
                contour_start_offset=args.drill_contour_start_offset,
                contour_spacing=args.drill_contour_spacing,
                contour_count=args.drill_contour_count,
            )
            print(f"drill holes: {hole_count}, generated {args.drill_style} segments: {segment_count} -> {args.output_dxf}")
            return 0

        if args.mode == "contour" and args.geometry_source == "pcbnew":
            kind = _detect_input_kind(args.source, args.input_format)
            if kind != "kicad":
                raise RuntimeError("--geometry-source pcbnew requires a KiCad source (.kicad_pcb or .kicad_pro).")
            board_path = _resolve_board_path(args.source)
            net_count, count = generate_contour_offset_dxf_from_board(
                board_path,
                args.output_dxf,
                start_offset_mm,
                spacing_mm,
                args.repetitions,
                args.layer_name,
                args.invert,
                auto_alternate_direction=not args.no_auto_alternate,
            )
            print(f"source nets: {net_count}, generated loops: {count} -> {args.output_dxf}")
            return 0

        with _prepared_input_dxf(args.source, args.input_format, args.kicad_layers) as source_dxf_path:
            is_kicad_source = _detect_input_kind(args.source, args.input_format) == "kicad"
            mirror_axis_mm = resolve_mirror_axis_mm(args.source, args.layer_name) if is_kicad_source else None
            edge_cuts_bbox_mm = resolve_edge_cuts_bbox_mm(args.source) if is_kicad_source else None
            if args.mode == "hatch":
                polys, count = generate_hatch_dxf(
                    source_dxf_path,
                    args.output_dxf,
                    args.angle,
                    spacing_mm,
                    args.layer_name,
                    alternate_nesting_hatch=args.alternate_nesting,
                    invert_alternate_nesting=args.invert_alternate_nesting,
                    multi_angle_hatch=args.multi_angle,
                    mirror_axis_mm=mirror_axis_mm,
                    edge_cuts_bbox_mm=edge_cuts_bbox_mm,
                )
                print(f"source polygons: {polys}, generated hatch segments: {count} -> {args.output_dxf}")
            else:
                polys, count = generate_contour_offset_dxf(
                    source_dxf_path,
                    args.output_dxf,
                    start_offset_mm,
                    spacing_mm,
                    args.repetitions,
                    args.layer_name,
                    args.invert,
                    auto_alternate_direction=not args.no_auto_alternate,
                    mirror_axis_mm=mirror_axis_mm,
                    edge_cuts_bbox_mm=edge_cuts_bbox_mm,
                )
                print(f"source polygons: {polys}, generated loops: {count} -> {args.output_dxf}")
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
