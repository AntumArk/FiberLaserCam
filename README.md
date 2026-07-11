# Fiber Laser DXF Hatch Tool

Local Python web app for converting selected closed DXF zones into hatch lines for fiber laser etching.

## Features

- Upload KiCad-generated DXF isolation files
- Detect closed zones from polylines, circles, and closed linework
- Visualize each zone with unique colors
- Click zones to select/unselect for hatching
- Preview generated hatch lines with controls:
  - Hatch angle (degrees)
  - Hatch spacing
  - Laser radius (inward offset)
- Export a DXF with hatch lines added on layer `HATCH_GEN`

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open: http://127.0.0.1:5000

## Notes

- Works best with DXF files containing closed contours or linework that forms closed polygons.
- Very complex DXF files may produce many tiny zones; use click selection to choose only desired areas.
