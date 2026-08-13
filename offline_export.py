from __future__ import annotations

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
    from contour_offsets import generate_contour_offset_loops
except ImportError:
    from kicad_plugin.contour_offsets import generate_contour_offset_loops


def _collect_polygons_from_dxf(doc: ezdxf.Drawing) -> list[list[tuple[float, float]]]:
    return collect_entities_as_polygons(doc)


def normalize_cli_path(path: str) -> str:
    return path.rstrip().removesuffix(";")


def _find_kicad_cli() -> str | None:
    for candidate in ("kicad-cli", "kicad-cli.exe"):
    resolved = shutil.which(candidate)
    if resolved:
        return normalize_cli_path(resolved)
    return None


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


def generate_kicad_drill_spiral_dxf(
    source_path: Path,
    output_dxf_path: Path,
    layer_name: str = "DRILL_GEN",
    spiral_turns: float = 1.75,
    spiral_inner_ratio: float = 0.10,
) -> tuple[int, int]:
    circles, segments = build_kicad_drill_spiral_geometry(
        source_path,
        spiral_turns=spiral_turns,
        spiral_inner_ratio=spiral_inner_ratio,
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
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    if auto_alternate_direction and polys:
        depths = compute_ring_nesting_depths(polys)
    else:
        depths = [0] * len(polys)

    loops: list[list[tuple[float, float]]] = []
    for poly, depth in zip(polys, depths):
        poly_invert = invert_direction != bool(depth % 2) if auto_alternate_direction else invert_direction
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions, poly_invert))

    if not loops:
        raise RuntimeError(
            "No contour loops generated from source DXF. "
            "Check selected export layers and contour parameters."
        )

    out_doc = ezdxf.new("R2000")
    if "$INSUNITS" in source_doc.header:
        out_doc.header["$INSUNITS"] = source_doc.header["$INSUNITS"]

    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in out_doc.header:
            del out_doc.header[header_key]

    if layer_name not in out_doc.layers:
        out_doc.layers.new(layer_name, dxfattribs={"color": 1})

    msp = out_doc.modelspace()
    for loop in loops:
        if len(loop) < 3:
            continue
        try:
            closed_loop = list(loop) + [loop[0]]
            msp.add_lwpolyline(closed_loop, close=False, dxfattribs={"layer": layer_name})
        except Exception:
            continue

    output_dxf_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(str(output_dxf_path))
    return len(polys), len(loops)


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

    loops: list[list[tuple[float, float]]] = []
    for poly, depth in zip(polys, depths):
        poly_invert = bool(depth % 2) if auto_alternate_direction else False
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions, poly_invert))

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
    )

    if not segments:
        raise RuntimeError(
            "No hatch segments generated from source DXF. "
            "Check selected export layers and hatch parameters."
        )

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
    parser.add_argument("--layer-name", default="F.Cu", help="Layer name for generated geometry (default: F.Cu).")
    parser.add_argument(
        "--spiral-turns",
        type=float,
        default=1.75,
        help="Turns per inward drill spiral arm, drill mode only (default: 1.75).",
    )
    parser.add_argument(
        "--spiral-inner-ratio",
        type=float,
        default=0.10,
        help="Spiral end radius as ratio of hole radius, drill mode only (default: 0.10).",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    start_offset_mm = args.start_offset / 1000.0
    spacing_mm = args.spacing / 1000.0
    try:
        if args.mode == "drill":
            kind = _detect_input_kind(args.source, args.input_format)
            if kind != "kicad":
                raise RuntimeError("Drill mode requires a KiCad source (.kicad_pcb or .kicad_pro).")
            hole_count, segment_count = generate_kicad_drill_spiral_dxf(
                args.source,
                args.output_dxf,
                layer_name=args.layer_name,
                spiral_turns=args.spiral_turns,
                spiral_inner_ratio=args.spiral_inner_ratio,
            )
            print(f"drill holes: {hole_count}, generated spiral segments: {segment_count} -> {args.output_dxf}")
            return 0

        with _prepared_input_dxf(args.source, args.input_format, args.kicad_layers) as source_dxf_path:
            if args.mode == "hatch":
                polys, count = generate_hatch_dxf(
                    source_dxf_path,
                    args.output_dxf,
                    args.angle,
                    spacing_mm,
                    args.layer_name,
                    alternate_nesting_hatch=args.alternate_nesting,
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
                )
                print(f"source polygons: {polys}, generated loops: {count} -> {args.output_dxf}")
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
