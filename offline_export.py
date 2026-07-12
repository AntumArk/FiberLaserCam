from __future__ import annotations

from pathlib import Path

import minidxf as ezdxf
from app_geometry import collect_entities_as_polygons

try:
    from contour_offsets import generate_contour_offset_loops
except ImportError:
    from kicad_plugin.contour_offsets import generate_contour_offset_loops


def _collect_polygons_from_dxf(doc: ezdxf.Drawing) -> list[list[tuple[float, float]]]:
    return collect_entities_as_polygons(doc)


def generate_contour_offset_dxf(
    source_dxf_path: Path,
    output_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
    layer_name: str = "HATCH_GEN",
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    loops: list[list[tuple[float, float]]] = []
    for poly in polys:
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions))

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


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="fiberlasercam-offline-export",
        description="Generate contour-offset loops from a source DXF, independent of KiCad.",
    )
    parser.add_argument("source_dxf", type=Path, help="Path to the source DXF file.")
    parser.add_argument("output_dxf", type=Path, help="Path to write the generated DXF file.")
    parser.add_argument("--start-offset", type=float, default=1.0, help="Offset of the first contour loop (default: 1.0).")
    parser.add_argument("--spacing", type=float, default=1.0, help="Spacing between successive contour loops (default: 1.0).")
    parser.add_argument("--repetitions", type=int, default=1, help="Number of contour loops to generate (default: 1).")
    parser.add_argument("--layer-name", default="HATCH_GEN", help="Layer name for generated loops (default: HATCH_GEN).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        polys, loops = generate_contour_offset_dxf(
            args.source_dxf,
            args.output_dxf,
            args.start_offset,
            args.spacing,
            args.repetitions,
            args.layer_name,
        )
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    print(f"source polygons: {polys}, generated loops: {loops} -> {args.output_dxf}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
