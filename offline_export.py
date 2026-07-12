from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import minidxf as ezdxf
from app_geometry import DEFAULT_MIN_HATCH_AREA, build_zone_payload_from_dxf_path, collect_entities_as_polygons, generate_hatch_for_selection
from app_sessions import UploadSession

try:
    from contour_offsets import generate_contour_offset_loops
except ImportError:
    from kicad_plugin.contour_offsets import generate_contour_offset_loops


def _collect_polygons_from_dxf(doc: ezdxf.Drawing) -> list[list[tuple[float, float]]]:
    return collect_entities_as_polygons(doc)


def _find_kicad_cli() -> str | None:
    for candidate in ("kicad-cli", "kicad-cli.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
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
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    loops: list[list[tuple[float, float]]] = []
    for poly in polys:
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions, invert_direction))

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
            msp.add_lwpolyline(loop, close=True, dxfattribs={"layer": layer_name})
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
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    loops: list[list[tuple[float, float]]] = []
    for poly in polys:
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions))

    return len(polys), len(loops)


def generate_hatch_dxf(
    source_dxf_path: Path,
    output_dxf_path: Path,
    angle: float,
    spacing: float,
    layer_name: str = "F.Cu",
    laser_radius: float = 0.01,
    min_area: float = DEFAULT_MIN_HATCH_AREA,
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
        session, selected_ids, angle, spacing, laser_radius, min_area, False
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
        "-m", "--mode", choices=["contour", "hatch"], default="contour",
        help="Generation mode: contour offset loops or angled hatch fill (default: contour).",
    )
    parser.add_argument(
        "--angle", type=float, default=45.0,
        help="Hatch line angle in degrees, hatch mode only (default: 45).",
    )
    parser.add_argument("--layer-name", default="F.Cu", help="Layer name for generated geometry (default: F.Cu).")
    parser.add_argument(
        "--invert", action="store_true",
        help="Invert offset direction, contour mode only (offset outward instead of inward).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    start_offset_mm = args.start_offset / 1000.0
    spacing_mm = args.spacing / 1000.0
    try:
        with _prepared_input_dxf(args.source, args.input_format, args.kicad_layers) as source_dxf_path:
            if args.mode == "hatch":
                polys, count = generate_hatch_dxf(
                    source_dxf_path,
                    args.output_dxf,
                    args.angle,
                    spacing_mm,
                    args.layer_name,
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
                )
                print(f"source polygons: {polys}, generated loops: {count} -> {args.output_dxf}")
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
