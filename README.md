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

## Preferred Workflow

The recommended entry point is the KiCad ActionPlugin.

1. Click the Fiber Laser launcher button in KiCad PCB Editor.
2. Pick the export layer. The default is `F.Cu`.
3. The plugin exports a temporary DXF, starts the local web server, and opens your browser.
4. Use the browser UI to select zones, tune hatch or contour settings, preview, and export.
5. Closing the browser tab stops the temporary server automatically.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open: http://127.0.0.1:5000

## Browser Defaults

- Hatch spacing defaults to `0.02 mm`.
- Contour offset start defaults to `0.02 mm`.
- Contour offset spacing defaults to `0.02 mm`.

## Notes

- Works best with DXF files containing closed contours or linework that forms closed polygons.
- Very complex DXF files may produce many tiny zones; use click selection to choose only desired areas.

## KiCad Plugin

A KiCad ActionPlugin lives in the repo root and launches the browser workflow from KiCad, exports the selected layer to DXF, and then hands off control to the local web app.

## Repository Layout

Keep the top-level bundle layout together when installing or packaging:

- `__init__.py`
- `fiber_laser_plugin.py`
- `offline_export.py`
- `contour_offsets.py`
- `icon_fiber_laser.xpm`
- `app.py`
- `templates/`
- `static/`

That is the clean install shape, so the browser app and the KiCad launcher stay side by side in the same folder tree.
