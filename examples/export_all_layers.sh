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
# Operation settings reference:
# - Drilling/edgecuts: 150 mm/s, 100% 20kHz, 4 contours @ 0.05mm inward, 30 passes
# - Routing: 2000 mm/s, 70% 100kHz, 10 contours @ 0.02mm, 15 passes
# - Pad clearance: 200 mm/s, <50% 100kHz, hatch 0.02mm spacing, 2-3 passes
#
# All contour layers use pcbnew-native geometry (--geometry-source pcbnew).

# 1) Drilling (4 contours inward, 0.05mm spacing, start offset 0.02mm)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/drill_holes.dxf" \
  --mode drill \
  --layer-name DRILL_GEN \
  --drill-style contour \
  --drill-contour-start-offset 0.02 \
  --drill-contour-spacing 0.05 \
  --drill-contour-count 4

# 2) Edge cuts - contour outward (4 contours, 0.05mm spacing)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/edge_cuts_contour.dxf" \
  --mode contour \
  --geometry-source pcbnew \
  --kicad-layers Edge.Cuts \
  --layer-name Edge.Cuts \
  --invert \
  -s 50 -i 50 -n 4

# 3) Edge cuts - hatch (board cleaning, same settings as pad clearance)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/edge_cuts_hatch.dxf" \
  --mode hatch \
  --kicad-layers Edge.Cuts \
  --layer-name Edge.Cuts \
  -i 20

# 4) Routing - front copper (10 contours, 0.02mm spacing)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/routing_f_cu.dxf" \
  --mode contour \
  --geometry-source pcbnew \
  --kicad-layers F.Cu \
  --layer-name F.Cu \
  -s 20 -i 20 -n 10

# 5) Routing - back copper (10 contours, 0.02mm spacing)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/routing_b_cu.dxf" \
  --mode contour \
  --geometry-source pcbnew \
  --kicad-layers B.Cu \
  --layer-name B.Cu \
  -s 20 -i 20 -n 10

# 6) Pad clearance - front copper (hatch, 0.02mm spacing, 2-3 passes on machine)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/pad_clearance_f_cu.dxf" \
  --mode hatch \
  --kicad-layers F.Cu \
  --layer-name F.Cu \
  -i 20

# 7) Pad clearance - back copper (hatch, 0.02mm spacing, 2-3 passes on machine)
"$APPIMAGE_PATH" "$BOARD_PATH" "$OUTPUT_DIR/pad_clearance_b_cu.dxf" \
  --mode hatch \
  --kicad-layers B.Cu \
  --layer-name B.Cu \
  -i 20
