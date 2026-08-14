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
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR" --export-all "$CONFIG_PATH"
