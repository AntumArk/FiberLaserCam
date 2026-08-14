#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <FiberLaserCam-AppImage> <board.kicad_pcb|board.kicad_pro> <output_dir> [default|pcbnew]"
  exit 1
fi

APPIMAGE_PATH="$1"
BOARD_PATH="$2"
OUTPUT_DIR="$3"
PROFILE="${4:-default}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$PROFILE" in
  default)
    CONFIG_PATH="$SCRIPT_DIR/export_all_config.default.json"
    ;;
  pcbnew)
    CONFIG_PATH="$SCRIPT_DIR/export_all_config.pcbnew_geometry.json"
    ;;
  *)
    echo "Unknown profile: $PROFILE (use: default | pcbnew)"
    exit 1
    ;;
esac

mkdir -p "$OUTPUT_DIR"

python - "$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR" "$CONFIG_PATH" <<'PY'
import json
import shlex
import subprocess
import sys
from pathlib import Path

appimage_path, board_path, output_dir, config_path = sys.argv[1:]
board = Path(board_path)
out_dir = Path(output_dir)
config = json.loads(Path(config_path).read_text())
layers = config.get("layers", [])
if not isinstance(layers, list) or not layers:
    raise SystemExit("Config must contain a non-empty 'layers' list.")


def mm_to_microns(mm: float) -> str:
    return str(float(mm) * 1000.0)


for entry in layers:
    kind = str(entry.get("kind", "layer"))
    if kind == "drill":
        output_name = str(entry.get("output", f"{board.stem}_Drill.dxf"))
        cmd = [
            appimage_path,
            board_path,
            str(out_dir / output_name),
            "--mode",
            "drill",
            "--layer-name",
            str(entry.get("layer_name", "DRILL_GEN")),
            "--drill-style",
            str(entry.get("style", "contour")),
        ]
        if str(entry.get("style", "contour")) == "spiral":
            cmd += [
                "--spiral-turns",
                str(entry.get("spiral_turns", 1.75)),
                "--spiral-inner-ratio",
                str(entry.get("spiral_inner_ratio", 0.10)),
            ]
        else:
            cmd += [
                "--drill-contour-start-offset",
                str(entry.get("contour_start_offset", 0.05)),
                "--drill-contour-spacing",
                str(entry.get("contour_spacing", 0.05)),
                "--drill-contour-count",
                str(entry.get("contour_count", 4)),
            ]
    else:
        layer = str(entry.get("layer", "")).strip()
        if not layer:
            raise SystemExit(f"Layer entry missing 'layer': {entry}")
        output_name = str(entry.get("output", f"{board.stem}_{layer.replace('.', '_').replace('/', '_')}.dxf"))
        mode = str(entry.get("mode", "contour"))
        cmd = [
            appimage_path,
            board_path,
            str(out_dir / output_name),
            "--mode",
            mode,
            "--layer-name",
            layer,
        ]
        if mode == "hatch":
            cmd += [
                "--angle",
                str(entry.get("angle", 45.0)),
                "--spacing",
                mm_to_microns(float(entry.get("spacing_mm", 0.02))),
            ]
            if bool(entry.get("alternate_nesting", False)):
                cmd.append("--alternate-nesting")
            if bool(entry.get("invert_alternate_nesting", False)):
                cmd.append("--invert-alternate-nesting")
            if bool(entry.get("multi_angle", False)):
                cmd.append("--multi-angle")
        else:
            cmd += [
                "--start-offset",
                mm_to_microns(float(entry.get("start_offset_mm", 0.02))),
                "--spacing",
                mm_to_microns(float(entry.get("spacing_mm", 0.02))),
                "--repetitions",
                str(entry.get("repetitions", 1)),
            ]
            if bool(entry.get("invert", False)):
                cmd.append("--invert")
            if not bool(entry.get("auto_alternate", True)):
                cmd.append("--no-auto-alternate")
            if str(entry.get("geometry_source", "dxf")) == "pcbnew":
                cmd += ["--geometry-source", "pcbnew"]

    print("+", shlex.join(cmd))
    subprocess.run(cmd, check=True)
PY
