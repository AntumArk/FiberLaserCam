from __future__ import annotations

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
        description="Generate contour-offset loops or hatch fill from a source DXF, independent of KiCad.",
    )
    parser.add_argument("source_dxf", type=Path, help="Path to the source DXF file.")
    parser.add_argument("output_dxf", type=Path, help="Path to write the generated DXF file.")
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
        if args.mode == "hatch":
            polys, count = generate_hatch_dxf(
                args.source_dxf,
                args.output_dxf,
                args.angle,
                spacing_mm,
                args.layer_name,
            )
            print(f"source polygons: {polys}, generated hatch segments: {count} -> {args.output_dxf}")
        else:
            polys, count = generate_contour_offset_dxf(
                args.source_dxf,
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
