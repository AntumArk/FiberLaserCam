#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <FiberLaserCam-AppImage> <board.kicad_pcb|board.kicad_pro> <output_dir>"
  exit 1
fi

APPIMAGE_PATH="$1"
BOARD_PATH="$2"
OUTPUT_DIR="$3"

mkdir -p "$OUTPUT_DIR"

#
# Machine setup values (speed/power/passes/extras) are done on the laser side.
# This script only prepares DXF geometry per operation/layer.
#
# Requested operation settings:
# - Drilling/edgecuts: 150 mm/s, 100% 20kHz, 4 contours @ 0.05mm inward, 30 passes
# - Routing: 2000 mm/s, 70% 100kHz, 10 contours @ 0.02mm, 15 passes
# - Spraying: 1 pass (spray paint as solder mask)
# - Pad clearance: 200 mm/s, <50% 100kHz, 0.02mm, 2-3 passes

# 1) Drilling (4 contours, 0.05mm inward)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/drill_holes.dxf" \
  --mode drill \
  --layer-name DRILL_GEN \
  --drill-style contour \
  --drill-contour-start-offset 0.05 \
  --drill-contour-spacing 0.05 \
  --drill-contour-count 4

# 2) Edge cuts (4 contours, 0.05mm spacing inward)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/edge_cuts.dxf" \
  --mode contour \
  --kicad-layers Edge.Cuts \
  --layer-name Edge.Cuts \
  -s 50 -i 50 -n 4

# 3) Routing - front copper (10 contours, 0.02mm spacing)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/routing_f_cu.dxf" \
  --mode contour \
  --kicad-layers F.Cu \
  --layer-name F.Cu \
  -s 20 -i 20 -n 10

# 4) Routing - back copper (10 contours, 0.02mm spacing)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/routing_b_cu.dxf" \
  --mode contour \
  --kicad-layers B.Cu \
  --layer-name B.Cu \
  -s 20 -i 20 -n 10

# 5) Spraying mask guide - front mask
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/spray_f_mask.dxf" \
  --mode hatch \
  -i 2000 \
  --kicad-layers F.Mask \
  --layer-name F.Mask

# 6) Spraying mask guide - back mask
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/spray_b_mask.dxf" \
  --mode hatch \
  -i 2000 \
  --kicad-layers B.Mask \
  --layer-name B.Mask

# 7) Pad clearance - front copper (use 2-3 passes on machine)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/pad_clearance_f_cu.dxf" \
  --mode contour \
  --kicad-layers F.Cu \
  --layer-name F.Cu \
  -s 20 -i 20 -n 3

# 8) Pad clearance - back copper (use 2-3 passes on machine)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/pad_clearance_b_cu.dxf" \
  --mode contour \
  --kicad-layers B.Cu \
  --layer-name B.Cu \
  -s 20 -i 20 -n 3
