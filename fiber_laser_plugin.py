from __future__ import annotations

import io
import json
import re
import subprocess
import shutil
import sys
import time
import uuid
from pathlib import Path

import pcbnew
import wx

import minidxf as ezdxf
import pcbnew_geometry
from offline_export import build_kicad_drill_geometry, DEFAULT_DRILL_STYLE, DRILL_STYLES
from app_geometry import (
    DEFAULT_MIN_HATCH_AREA,
    build_contour_loops_for_selection,
    build_zone_payload_from_dxf_path,
    generate_contour_offsets_for_selection,
    generate_hatch_for_selection,
    resolve_spacing,
    sanitize_segments,
)
from app_sessions import UploadSession
from contour_offsets import (
    corner_alignment_mark_segments,
    is_back_layer,
    loop_to_segments,
    mirror_ring_x,
    mirror_segments_x,
)


PLUGIN_DIR = Path(__file__).resolve().parent
TEMP_DXF_DIR = PLUGIN_DIR / "temp_dxf"
SETTINGS_KEY = "layer_settings_json"
LAST_LAYER_KEY = "last_layer"


DEFAULT_LAYER_SETTINGS: dict[str, object] = {
    "mode": "contour_offsets",
    "angle": 45.0,
    "spacing": 0.02,
    "useManualSpacing": True,
    "laserRadius": 0.01,
    "minArea": 0.30,
    "offsetStart": 0.02,
    "offsetSpacing": 0.02,
    "offsetCount": 3,
    "invertOffsetDirection": False,
    "autoAlternateContourDirection": True,
    "hatchAll": True,
    "outerZoneOnly": False,
    "alternateNestingHatch": False,
    "invertAlternateNesting": False,
    "multiAngleHatch": False,
    "spiralTurns": 1.75,
    "spiralInnerRatio": 0.10,
    "drillStyle": DEFAULT_DRILL_STYLE,
    "drillContourStart": 0.05,
    "drillContourSpacing": 0.05,
    "drillContourCount": 4,
    "usePcbnewGeometry": False,
}


def _default_layer_settings_for(layer_name: str) -> dict[str, object]:
    base = dict(DEFAULT_LAYER_SETTINGS)
    normalized = layer_name.strip().lower()
    if normalized == "edge.cuts":
        base["mode"] = "hatch"
        base["hatchAll"] = True
        base["outerZoneOnly"] = True
    elif normalized in {"f.mask", "b.mask"}:
        # Solder mask layers are exported as hatch fill (not isolation contours)
        # so the laser clears the paint/mask coating inside the openings.
        base["mode"] = "hatch"
        base["hatchAll"] = True
        base["outerZoneOnly"] = False
    return _sanitize_layer_settings(base)


def _coerce_bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return fallback


def _coerce_float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _coerce_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _sanitize_layer_settings(raw: dict[str, object] | None) -> dict[str, object]:
    merged = dict(DEFAULT_LAYER_SETTINGS)
    if isinstance(raw, dict):
        merged.update(raw)

    mode = str(merged.get("mode", DEFAULT_LAYER_SETTINGS["mode"]))
    if mode not in {"hatch", "contour_offsets", "drill"}:
        mode = str(DEFAULT_LAYER_SETTINGS["mode"])

    drill_style = str(merged.get("drillStyle", DEFAULT_LAYER_SETTINGS["drillStyle"]))
    if drill_style not in DRILL_STYLES:
        drill_style = str(DEFAULT_LAYER_SETTINGS["drillStyle"])

    clean: dict[str, object] = {
        "mode": mode,
        "angle": _coerce_float(merged.get("angle"), float(DEFAULT_LAYER_SETTINGS["angle"])),
        "spacing": _coerce_float(merged.get("spacing"), float(DEFAULT_LAYER_SETTINGS["spacing"])),
        "useManualSpacing": _coerce_bool(merged.get("useManualSpacing"), bool(DEFAULT_LAYER_SETTINGS["useManualSpacing"])),
        "laserRadius": _coerce_float(merged.get("laserRadius"), float(DEFAULT_LAYER_SETTINGS["laserRadius"])),
        "minArea": _coerce_float(merged.get("minArea"), float(DEFAULT_LAYER_SETTINGS["minArea"])),
        "offsetStart": _coerce_float(merged.get("offsetStart"), float(DEFAULT_LAYER_SETTINGS["offsetStart"])),
        "offsetSpacing": _coerce_float(merged.get("offsetSpacing"), float(DEFAULT_LAYER_SETTINGS["offsetSpacing"])),
        "offsetCount": max(1, _coerce_int(merged.get("offsetCount"), int(DEFAULT_LAYER_SETTINGS["offsetCount"]))),
        "invertOffsetDirection": _coerce_bool(merged.get("invertOffsetDirection"), bool(DEFAULT_LAYER_SETTINGS["invertOffsetDirection"])),
        "autoAlternateContourDirection": _coerce_bool(
            merged.get("autoAlternateContourDirection"), bool(DEFAULT_LAYER_SETTINGS["autoAlternateContourDirection"])
        ),
        "hatchAll": _coerce_bool(merged.get("hatchAll"), bool(DEFAULT_LAYER_SETTINGS["hatchAll"])),
        "outerZoneOnly": _coerce_bool(merged.get("outerZoneOnly"), bool(DEFAULT_LAYER_SETTINGS["outerZoneOnly"])),
        "alternateNestingHatch": _coerce_bool(merged.get("alternateNestingHatch"), bool(DEFAULT_LAYER_SETTINGS["alternateNestingHatch"])),
        "invertAlternateNesting": _coerce_bool(merged.get("invertAlternateNesting"), bool(DEFAULT_LAYER_SETTINGS["invertAlternateNesting"])),
        "multiAngleHatch": _coerce_bool(merged.get("multiAngleHatch"), bool(DEFAULT_LAYER_SETTINGS["multiAngleHatch"])),
        "spiralTurns": _coerce_float(merged.get("spiralTurns"), float(DEFAULT_LAYER_SETTINGS["spiralTurns"])),
        "spiralInnerRatio": _coerce_float(merged.get("spiralInnerRatio"), float(DEFAULT_LAYER_SETTINGS["spiralInnerRatio"])),
        "drillStyle": drill_style,
        "drillContourStart": _coerce_float(merged.get("drillContourStart"), float(DEFAULT_LAYER_SETTINGS["drillContourStart"])),
        "drillContourSpacing": _coerce_float(merged.get("drillContourSpacing"), float(DEFAULT_LAYER_SETTINGS["drillContourSpacing"])),
        "drillContourCount": max(1, _coerce_int(merged.get("drillContourCount"), int(DEFAULT_LAYER_SETTINGS["drillContourCount"]))),
        "usePcbnewGeometry": _coerce_bool(merged.get("usePcbnewGeometry"), bool(DEFAULT_LAYER_SETTINGS["usePcbnewGeometry"])),
    }
    return clean


def _load_all_layer_settings() -> dict[str, dict[str, object]]:
    config = wx.Config("FiberLaserCam")
    raw_json = config.Read(SETTINGS_KEY, "")
    if not raw_json:
        return {}
    try:
        decoded = json.loads(raw_json)
    except Exception:
        return {}
    if not isinstance(decoded, dict):
        return {}

    result: dict[str, dict[str, object]] = {}
    for layer, settings in decoded.items():
        if not isinstance(layer, str):
            continue
        result[layer] = _sanitize_layer_settings(settings if isinstance(settings, dict) else None)
    return result


def _save_all_layer_settings(all_settings: dict[str, dict[str, object]]) -> None:
    config = wx.Config("FiberLaserCam")
    config.Write(SETTINGS_KEY, json.dumps(all_settings))
    config.Flush()


def _save_last_layer(layer_name: str) -> None:
    config = wx.Config("FiberLaserCam")
    config.Write(LAST_LAYER_KEY, layer_name)
    config.Flush()


def _load_last_layer(default_value: str) -> str:
    config = wx.Config("FiberLaserCam")
    layer = config.Read(LAST_LAYER_KEY, default_value)
    return layer or default_value


def _message(title: str, text: str, style: int) -> None:
    wx.MessageBox(text, title, style)


def _find_kicad_parent_window() -> wx.Window | None:
    try:
        top_levels = list(wx.GetTopLevelWindows())
    except Exception:
        return None

    for window in top_levels:
        try:
            title = window.GetTitle().lower()
        except Exception:
            continue
        if "pcb editor" in title or "pcbnew" in title:
            return window

    for window in top_levels:
        try:
            if window.IsShown():
                return window
        except Exception:
            continue

    return None


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


def _run_kicad_dxf_export(kicad_cli: str, board_path: Path, output_path: Path, layers: str) -> None:
    command = [
        kicad_cli,
        "pcb",
        "export",
        "dxf",
        str(board_path),
        "-o",
        str(output_path),
        "--layers",
        layers,
        "--mode-single",
        "--output-units",
        "mm",
        "--use-contours",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "Unknown export failure."
        raise RuntimeError(details)


def _export_kicad_layer_via_pcbnew(
    layer: str, output_path: Path
) -> tuple[dict[int, object], dict[str, int]]:
    """pcbnew-native counterpart to _run_kicad_dxf_export: writes a DXF of one
    copper layer's net-merged polygons directly from the live board (no
    kicad-cli subprocess). Same-net pads/tracks/zones are merged with KiCad's
    own SHAPE_POLY_SET.Simplify(), so touching same-net features never
    produce separate/overlapping polygons the way a raw kicad-cli DXF export
    can. Downstream zone-selection, offsetting, and hatching are unchanged --
    only the shape of the exported polygons differs from the DXF path.

    Also returns ``(net_polys, zone_net_map)``: the per-net merged
    ``SHAPE_POLY_SET`` dict, and a ``zone_id -> net_code`` mapping (zone ids
    assigned in the same 1-based, non-degenerate-ring order that
    ``build_zone_payload_from_dxf_path`` uses when it re-parses the DXF this
    function writes). Contour mode uses these to offset with KiCad's own
    Inflate() directly instead of going back through the slower, DXF-derived
    per-vertex miter-join offsetting -- without them, callers would have to
    re-extract net polygons from the board a second time."""
    board = pcbnew.GetBoard()
    layer_id = pcbnew_geometry.resolve_layer_id(board, layer)
    net_polys = pcbnew_geometry.build_net_polygons_for_layer(board, layer_id)

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    if layer not in doc.layers:
        doc.layers.new(layer, dxfattribs={"color": 1})

    msp = doc.modelspace()
    zone_net_map: dict[str, int] = {}
    zone_index = 0
    for net_code, polyset in net_polys.items():
        for ring in pcbnew_geometry.polyset_to_rings(polyset):
            if len(ring) < 3:
                continue
            zone_index += 1
            zone_net_map[str(zone_index)] = net_code
            closed = list(ring) + [ring[0]]
            msp.add_lwpolyline(closed, close=False, dxfattribs={"layer": layer})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(output_path))
    return net_polys, zone_net_map


class PreviewCanvas(wx.Panel):
    #: Zoom multiplier applied per mouse-wheel notch.
    ZOOM_STEP = 1.15
    MIN_ZOOM = 0.05
    MAX_ZOOM = 200.0

    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.zones: dict[str, list[list[float]]] = {}
        self.selected: set[str] = set()
        self.segments: list[list[list[float]]] = []
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._dragging = False
        self._drag_last: tuple[int, int] | None = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_mouse_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_MIDDLE_DOWN, self._on_reset_view)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_reset_view)

    def set_data(self, zones: dict[str, list[list[float]]], selected: set[str], segments: list[list[list[float]]]) -> None:
        self.zones = zones
        self.selected = selected
        self.segments = segments
        self.Refresh(False)

    def reset_view(self) -> None:
        """Reset zoom/pan back to the auto-fit view. Call this on layer changes."""
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.Refresh(False)

    def _on_reset_view(self, _event) -> None:
        self.reset_view()

    def _collect_bounds(self) -> tuple[float, float, float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for pts in self.zones.values():
            for p in pts:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
        for seg in self.segments:
            for p in seg:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def _base_transform(self) -> tuple[float, float, float, float, float] | None:
        """Compute the auto-fit (minx, miny, scale, width, height) for current bounds/size."""
        bounds = self._collect_bounds()
        if bounds is None:
            return None
        minx, miny, maxx, maxy = bounds
        width, height = self.GetClientSize()
        if width <= 2 or height <= 2:
            return None

        span_x = max(maxx - minx, 1e-6)
        span_y = max(maxy - miny, 1e-6)
        margin = 16.0
        base_scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
        return (minx, miny, base_scale, width, height)

    def _world_to_screen(self, x: float, y: float) -> tuple[float, float] | None:
        transform = self._base_transform()
        if transform is None:
            return None
        minx, miny, base_scale, width, height = transform
        margin = 16.0
        scale = base_scale * self._zoom
        sx = margin + self._pan_x + ((x - minx) * scale)
        sy = height - (margin + self._pan_y + ((y - miny) * scale))
        return (sx, sy)

    def _screen_to_world(self, sx: float, sy: float) -> tuple[float, float] | None:
        transform = self._base_transform()
        if transform is None:
            return None
        minx, miny, base_scale, width, height = transform
        margin = 16.0
        scale = base_scale * self._zoom
        if scale <= 1e-12:
            return None
        x = minx + ((sx - margin - self._pan_x) / scale)
        y = miny + ((height - sy - margin - self._pan_y) / scale)
        return (x, y)

    def _on_mouse_wheel(self, event: "wx.MouseEvent") -> None:
        rotation = event.GetWheelRotation()
        if rotation == 0:
            return

        mouse_pos = event.GetPosition()
        world_before = self._screen_to_world(float(mouse_pos.x), float(mouse_pos.y))

        factor = self.ZOOM_STEP if rotation > 0 else (1.0 / self.ZOOM_STEP)
        new_zoom = min(self.MAX_ZOOM, max(self.MIN_ZOOM, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom

        if world_before is not None:
            # Keep the point under the cursor stationary on screen after zooming.
            screen_after = self._world_to_screen(world_before[0], world_before[1])
            if screen_after is not None:
                self._pan_x += float(mouse_pos.x) - screen_after[0]
                self._pan_y -= float(mouse_pos.y) - screen_after[1]

        self.Refresh(False)

    def _on_left_down(self, event: "wx.MouseEvent") -> None:
        self._dragging = True
        self._drag_last = (event.GetPosition().x, event.GetPosition().y)
        if not self.HasCapture():
            self.CaptureMouse()

    def _on_left_up(self, _event) -> None:
        self._dragging = False
        self._drag_last = None
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_capture_lost(self, _event) -> None:
        self._dragging = False
        self._drag_last = None

    def _on_motion(self, event: "wx.MouseEvent") -> None:
        if not self._dragging or self._drag_last is None or not event.Dragging() or not event.LeftIsDown():
            return
        pos = event.GetPosition()
        dx = pos.x - self._drag_last[0]
        dy = pos.y - self._drag_last[1]
        self._drag_last = (pos.x, pos.y)
        self._pan_x += dx
        self._pan_y -= dy
        self.Refresh(False)

    def _on_paint(self, _event) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.Clear()

        transform = self._base_transform()
        if transform is None:
            return
        minx, miny, base_scale, width, height = transform
        scale = base_scale * self._zoom
        margin = 16.0

        def sx(x: float) -> int:
            return int(margin + self._pan_x + ((x - minx) * scale))

        def sy(y: float) -> int:
            return int(height - (margin + self._pan_y + ((y - miny) * scale)))

        zone_pen = wx.Pen(wx.Colour(90, 90, 90), 1)
        selected_pen = wx.Pen(wx.Colour(0, 140, 220), 2)
        hatch_pen = wx.Pen(wx.Colour(220, 70, 60), 1)

        for zid, pts in self.zones.items():
            if len(pts) < 2:
                continue
            poly_pts = [wx.Point(sx(float(p[0])), sy(float(p[1]))) for p in pts]
            poly_pts.append(poly_pts[0])
            dc.SetPen(selected_pen if zid in self.selected else zone_pen)
            dc.DrawLines(poly_pts)

        dc.SetPen(hatch_pen)
        for seg in self.segments:
            if len(seg) != 2:
                continue
            p1, p2 = seg
            dc.DrawLine(sx(float(p1[0])), sy(float(p1[1])), sx(float(p2[0])), sy(float(p2[1])))


def _build_local_session_from_dxf(raw_output_path: Path) -> tuple[UploadSession, list[dict]]:
    zones, zone_map = build_zone_payload_from_dxf_path(str(raw_output_path))
    now = time.time()
    session = UploadSession(
        path=str(raw_output_path),
        zone_map=zone_map,
        zone_payload=zones,
        created_ts=now,
        last_access_ts=now,
        temp_paths=[str(raw_output_path)],
    )
    return session, zones


# Standard KiCad layers considered by the "Export All" button, in export order.
EXPORT_ALL_LAYER_ORDER = ["F.Cu", "B.Cu", "Edge.Cuts", "F.Mask", "B.Mask"]
EXPORT_ALL_DRILL_LAYER = "Drill"


def _export_all_target_layers(layer_choices: list[str]) -> list[str]:
    """Pick the subset of EXPORT_ALL_LAYER_ORDER actually present on the board."""
    lower_map = {lc.lower(): lc for lc in layer_choices}
    return [lower_map[name.lower()] for name in EXPORT_ALL_LAYER_ORDER if name.lower() in lower_map]


def _build_session_for_kicad_layer(
    kicad_cli: str, board_path: Path, layer: str, use_pcbnew: bool = False
) -> tuple[UploadSession, list[str], Path, dict[str, object] | None]:
    """Export a single KiCad layer to a temp DXF and build a session + zone id list from it.

    When ``use_pcbnew`` is True, the layer is exported via the pcbnew-native
    path (_export_kicad_layer_via_pcbnew, no kicad-cli subprocess, net-merged
    polygons) instead of kicad-cli, and the returned 4th element is a
    ``{"net_polys": ..., "zone_net_map": ...}`` dict letting contour mode
    offset directly with pcbnew's Inflate() instead of the slower DXF-derived
    per-vertex offsetting (``None`` when ``use_pcbnew`` is False).

    The caller is responsible for deleting the returned temp DXF path once done with it.
    On failure, this function cleans up the temp DXF itself before re-raising, since the
    caller never receives a path to clean up in that case.
    """
    TEMP_DXF_DIR.mkdir(parents=True, exist_ok=True)
    raw_output_path = TEMP_DXF_DIR / f"{board_path.stem}-fiber-export-{uuid.uuid4().hex}.dxf"
    pcbnew_ctx: dict[str, object] | None = None
    try:
        if use_pcbnew:
            net_polys, zone_net_map = _export_kicad_layer_via_pcbnew(layer, raw_output_path)
            pcbnew_ctx = {"net_polys": net_polys, "zone_net_map": zone_net_map}
        else:
            _run_kicad_dxf_export(kicad_cli, board_path, raw_output_path, layer)
        session, zones = _build_local_session_from_dxf(raw_output_path)
    except Exception:
        if raw_output_path.exists():
            try:
                raw_output_path.unlink()
            except Exception:
                pass
        raise
    zone_ids = [str(z.get("id", "")).strip() for z in zones if str(z.get("id", "")).strip()]
    return session, zone_ids, raw_output_path, pcbnew_ctx


def _payload_from_settings(settings: dict[str, object], *, board_path: Path, selected_ids: list[str]) -> dict[str, object]:
    """Build a preview/export payload dict from a sanitized layer settings dict."""
    return {
        "selectedIds": selected_ids,
        "boardPath": str(board_path),
        "mode": settings["mode"],
        "angle": settings["angle"],
        "spacing": settings["spacing"],
        "useManualSpacing": settings["useManualSpacing"],
        "laserRadius": settings["laserRadius"],
        "minArea": settings["minArea"],
        "drillStyle": settings["drillStyle"],
        "drillContourStart": settings["drillContourStart"],
        "drillContourSpacing": settings["drillContourSpacing"],
        "drillContourCount": settings["drillContourCount"],
        "spiralTurns": settings["spiralTurns"],
        "spiralInnerRatio": settings["spiralInnerRatio"],
        "outerZoneOnly": settings["outerZoneOnly"],
        "alternateNestingHatch": settings["alternateNestingHatch"],
        "invertAlternateNesting": settings["invertAlternateNesting"],
        "multiAngleHatch": settings["multiAngleHatch"],
        "offsetStart": settings["offsetStart"],
        "offsetSpacing": settings["offsetSpacing"],
        "offsetCount": settings["offsetCount"],
        "invertOffsetDirection": settings["invertOffsetDirection"],
        "autoAlternateContourDirection": settings["autoAlternateContourDirection"],
    }


def _selected_net_polys(selected_ids, pcbnew_ctx: dict[str, object] | None) -> dict[int, object] | None:
    """Resolve the selected zone ids back to their originating net codes and
    return the corresponding subset of ``pcbnew_ctx["net_polys"]``, restricted
    to only the currently-selected nets (mirroring how the DXF-based path
    only lets selected zones interact for overcut prevention). Returns
    ``None`` when ``pcbnew_ctx`` is unavailable (i.e. not using pcbnew-native
    geometry for this layer)."""
    if pcbnew_ctx is None:
        return None
    zone_net_map = pcbnew_ctx["zone_net_map"]
    net_polys = pcbnew_ctx["net_polys"]
    selected = {str(zid) for zid in selected_ids}
    net_codes = {zone_net_map[zid] for zid in selected if zid in zone_net_map}
    return {code: net_polys[code] for code in net_codes if code in net_polys}


def _mirror_axis_for_layer(kicad_layer_name: str | None) -> float | None:
    """Resolve the X (mm) axis to mirror ``kicad_layer_name``'s geometry
    across, or None if no mirroring applies.

    Only back-side layers (``B.Cu``, ``B.Mask``, etc.) need mirroring, across
    the live board's own Edge.Cuts bbox center, so the preview and exported
    DXF line up with the board once physically flipped over to work on its
    back side (see ``pcbnew_geometry.get_board_mirror_axis_mm``). The live
    GUI plugin always has ``pcbnew`` available (it runs inside KiCad), so
    this works regardless of whether "Use pcbnew-native geometry" is
    checked for the current layer.
    """
    if not kicad_layer_name or not is_back_layer(kicad_layer_name):
        return None
    return pcbnew_geometry.get_board_mirror_axis_mm(pcbnew.GetBoard())


def _edge_cuts_bbox_mm() -> tuple[float, float, float, float] | None:
    """Return the live board's own Edge.Cuts bounding box (mm), or None if
    the board has no edge-cuts geometry.

    Used to derive tiny corner alignment marks (see
    ``contour_offsets.corner_alignment_mark_segments``) added to every
    exported DXF file so they all share an identical bounding box
    regardless of which layer's geometry each file actually contains --
    working around fiber-laser controllers that compute their own bounding
    box per loaded file and center/align on it, which would otherwise throw
    a multi-layer job out of registration.
    """
    return pcbnew_geometry.get_board_edge_cuts_bbox_mm(pcbnew.GetBoard())


def _add_corner_alignment_marks(modelspace, layer_name: str) -> None:
    edge_cuts_bbox_mm = _edge_cuts_bbox_mm()
    if edge_cuts_bbox_mm is None:
        return
    for seg in corner_alignment_mark_segments(edge_cuts_bbox_mm):
        p1, p2 = seg
        modelspace.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})


def _generate_preview_segments(
    session: UploadSession,
    payload: dict[str, object],
    *,
    outer_only_override: bool | None = None,
    pcbnew_ctx: dict[str, object] | None = None,
    kicad_layer_name: str | None = None,
) -> list[list[list[float]]]:
    selected_ids = payload.get("selectedIds") or []
    mode = str(payload.get("mode", "hatch"))

    if mode == "drill":
        board_path_raw = payload.get("boardPath", "")
        board_path = Path(str(board_path_raw))
        drill_style = str(payload.get("drillStyle", DEFAULT_DRILL_STYLE))
        spiral_turns = float(payload.get("spiralTurns", 1.75))
        spiral_inner_ratio = float(payload.get("spiralInnerRatio", 0.10))
        contour_start = float(payload.get("drillContourStart", 0.05))
        contour_spacing = float(payload.get("drillContourSpacing", 0.05))
        contour_count = int(payload.get("drillContourCount", 4))
        _, segments = build_kicad_drill_geometry(
            board_path,
            style=drill_style,
            spiral_turns=spiral_turns,
            spiral_inner_ratio=spiral_inner_ratio,
            contour_start_offset=contour_start,
            contour_spacing=contour_spacing,
            contour_count=contour_count,
        )
        return segments

    mirror_axis_mm = _mirror_axis_for_layer(kicad_layer_name)

    if mode == "contour_offsets":
        start_offset = float(payload.get("offsetStart", 0.2))
        offset_spacing = float(payload.get("offsetSpacing", 0.2))
        offset_count = int(payload.get("offsetCount", 3))
        invert_offset_direction = bool(payload.get("invertOffsetDirection", False))
        auto_alternate_direction = bool(payload.get("autoAlternateContourDirection", True))

        selected_net_polys = _selected_net_polys(selected_ids, pcbnew_ctx)
        if selected_net_polys is not None:
            loops = pcbnew_geometry.generate_contour_offsets_from_net_polys(
                selected_net_polys,
                start_offset,
                offset_spacing,
                offset_count,
                invert_direction=invert_offset_direction,
                auto_alternate_direction=auto_alternate_direction,
                mirror_axis_mm=mirror_axis_mm,
            )
            segments: list[list[list[float]]] = []
            for points, closed in loops:
                segments.extend(loop_to_segments(points, closed=closed))
            min_seg_len = max(offset_spacing * 0.02, 1e-6)
            segments, _dropped_tiny, _dropped_dupe = sanitize_segments(
                segments, min_length=min_seg_len, quant_grid=1e-5
            )
            return segments

        segments, _ = generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
            invert_offset_direction=invert_offset_direction,
            auto_alternate_direction=auto_alternate_direction,
        )
        if mirror_axis_mm is not None:
            segments = mirror_segments_x(segments, mirror_axis_mm)
        return segments

    angle = float(payload.get("angle", 45))
    laser_radius = float(payload.get("laserRadius", 0.01))
    min_area = float(payload.get("minArea", DEFAULT_MIN_HATCH_AREA))
    use_manual_spacing = bool(payload.get("useManualSpacing", False))
    spacing_value = payload.get("spacing", None)
    spacing_float = None if spacing_value in (None, "") else float(spacing_value)
    spacing, spacing_error = resolve_spacing(use_manual_spacing, spacing_float, laser_radius)
    if spacing_error is not None or spacing is None:
        raise RuntimeError(spacing_error or "Invalid hatch spacing")

    outer_zone_only = bool(payload.get("outerZoneOnly", False))
    alternate_nesting_hatch = bool(payload.get("alternateNestingHatch", False))
    invert_alternate_nesting = bool(payload.get("invertAlternateNesting", False))
    multi_angle_hatch = bool(payload.get("multiAngleHatch", False))
    if outer_only_override is not None:
        outer_zone_only = bool(outer_only_override)
    if outer_zone_only:
        alternate_nesting_hatch = False

    segments, _ = generate_hatch_for_selection(
        session,
        selected_ids,
        angle,
        spacing,
        laser_radius,
        min_area,
        outer_zone_only,
        alternate_nesting_hatch,
        invert_alternate_nesting,
        multi_angle_hatch,
    )
    if mirror_axis_mm is not None:
        segments = mirror_segments_x(segments, mirror_axis_mm)
    return segments


def _generate_export_dxf_bytes(
    session: UploadSession,
    payload: dict[str, object],
    *,
    outer_only_override: bool | None = None,
    pcbnew_ctx: dict[str, object] | None = None,
    kicad_layer_name: str | None = None,
) -> bytes:
    selected_ids = payload.get("selectedIds") or []
    mode = str(payload.get("mode", "hatch"))

    if mode == "drill":
        board_path_raw = payload.get("boardPath", "")
        board_path = Path(str(board_path_raw))
        drill_style = str(payload.get("drillStyle", DEFAULT_DRILL_STYLE))
        spiral_turns = float(payload.get("spiralTurns", 1.75))
        spiral_inner_ratio = float(payload.get("spiralInnerRatio", 0.10))
        contour_start = float(payload.get("drillContourStart", 0.05))
        contour_spacing = float(payload.get("drillContourSpacing", 0.05))
        contour_count = int(payload.get("drillContourCount", 4))
        circles, segments = build_kicad_drill_geometry(
            board_path,
            style=drill_style,
            spiral_turns=spiral_turns,
            spiral_inner_ratio=spiral_inner_ratio,
            contour_start_offset=contour_start,
            contour_spacing=contour_spacing,
            contour_count=contour_count,
        )

        doc = ezdxf.new("R2000")
        doc.header["$INSUNITS"] = 4
        layer_name = "DRILL_GEN"
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={"color": 1})

        modelspace = doc.modelspace()
        for circle in circles:
            closed_circle = list(circle) + [circle[0]]
            modelspace.add_lwpolyline(closed_circle, close=False, dxfattribs={"layer": layer_name})
        for seg in segments:
            p1, p2 = seg
            modelspace.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

        _add_corner_alignment_marks(modelspace, layer_name)

        stream = io.StringIO()
        doc.write(stream)
        return stream.getvalue().encode("utf-8")

    mirror_axis_mm = _mirror_axis_for_layer(kicad_layer_name)

    if mode == "contour_offsets":
        start_offset = float(payload.get("offsetStart", 0.2))
        offset_spacing = float(payload.get("offsetSpacing", 0.2))
        offset_count = int(payload.get("offsetCount", 3))
        invert_offset_direction = bool(payload.get("invertOffsetDirection", False))
        auto_alternate_direction = bool(payload.get("autoAlternateContourDirection", True))

        selected_net_polys = _selected_net_polys(selected_ids, pcbnew_ctx)
        if selected_net_polys is not None:
            loops = pcbnew_geometry.generate_contour_offsets_from_net_polys(
                selected_net_polys,
                start_offset,
                offset_spacing,
                offset_count,
                invert_direction=invert_offset_direction,
                auto_alternate_direction=auto_alternate_direction,
                mirror_axis_mm=mirror_axis_mm,
            )
        else:
            loops = build_contour_loops_for_selection(
                session,
                selected_ids,
                start_offset,
                offset_spacing,
                offset_count,
                invert_offset_direction=invert_offset_direction,
                auto_alternate_direction=auto_alternate_direction,
            )
            if mirror_axis_mm is not None:
                loops = [(mirror_ring_x(points, mirror_axis_mm), closed) for points, closed in loops]
    else:
        segments = _generate_preview_segments(
            session,
            payload,
            outer_only_override=outer_only_override,
            kicad_layer_name=kicad_layer_name,
        )
        loops = []

    source_doc = ezdxf.readfile(session.path)
    source_version = "R2000" if mode == "contour_offsets" else getattr(source_doc, "dxfversion", "R2010") or "R2010"
    doc = ezdxf.new(source_version)
    if "$INSUNITS" in source_doc.header:
        doc.header["$INSUNITS"] = source_doc.header["$INSUNITS"]

    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in doc.header:
            del doc.header[header_key]

    layer_name = "HATCH_GEN"
    if layer_name not in doc.layers:
        doc.layers.new(layer_name, dxfattribs={"color": 1})

    modelspace = doc.modelspace()
    if mode == "contour_offsets":
        for points, closed in loops:
            min_points = 3 if closed else 2
            if len(points) < min_points:
                continue
            try:
                dxf_points = list(points) + [points[0]] if closed else list(points)
                modelspace.add_lwpolyline(dxf_points, close=False, dxfattribs={"layer": layer_name})
            except Exception:
                dxf_points = list(points) + [points[0]] if closed else list(points)
                modelspace.add_polyline2d(dxf_points, close=False, dxfattribs={"layer": layer_name})
    else:
        for seg in segments:
            p1, p2 = seg
            modelspace.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

    _add_corner_alignment_marks(modelspace, layer_name)

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


class FiberLaserWorkspaceDialog(wx.Dialog):
    """Single combined window: settings, zone selection, and live preview."""

    def __init__(self, parent, board_path: Path, kicad_cli: str, layer_choices: list[str], initial_layer: str):
        super().__init__(
            parent,
            title="Fiber Laser Export",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.board_path = board_path
        self.kicad_cli = kicad_cli
        self.layer_choices = layer_choices
        self.session: UploadSession | None = None
        self.zone_map: dict[str, list[list[float]]] = {}
        self.zone_order: list[str] = []
        self.raw_output_path: Path | None = None
        self._pcbnew_ctx: dict[str, object] | None = None
        self._all_layer_settings = _load_all_layer_settings()
        self._suspend_events = False

        outer_panel = wx.Panel(self)
        root = wx.BoxSizer(wx.HORIZONTAL)

        settings_scroll = wx.ScrolledWindow(outer_panel, style=wx.VSCROLL)
        settings_scroll.SetScrollRate(0, 12)
        self.settings_scroll = settings_scroll

        left = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=10)
        grid.AddGrowableCol(1, 1)

        panel = settings_scroll
        self.layer_choice = wx.Choice(panel, choices=layer_choices)
        self.mode_choice = wx.Choice(panel, choices=["hatch", "contour_offsets", "drill"])
        self.angle_ctrl = wx.TextCtrl(panel)
        self.spacing_ctrl = wx.TextCtrl(panel)
        self.manual_spacing_ctrl = wx.CheckBox(panel, label="Use manual hatch spacing")
        self.radius_ctrl = wx.TextCtrl(panel)
        self.min_area_ctrl = wx.TextCtrl(panel)
        self.drill_style_choice = wx.Choice(panel, choices=list(DRILL_STYLES))
        self.drill_contour_start_ctrl = wx.TextCtrl(panel)
        self.drill_contour_spacing_ctrl = wx.TextCtrl(panel)
        self.drill_contour_count_ctrl = wx.TextCtrl(panel)
        self.spiral_turns_ctrl = wx.TextCtrl(panel)
        self.spiral_inner_ratio_ctrl = wx.TextCtrl(panel)
        self.offset_start_ctrl = wx.TextCtrl(panel)
        self.offset_spacing_ctrl = wx.TextCtrl(panel)
        self.offset_count_ctrl = wx.TextCtrl(panel)
        self.invert_offset_direction_ctrl = wx.CheckBox(panel, label="Invert offset direction (toward interior)")
        self.auto_alternate_direction_ctrl = wx.CheckBox(panel, label="Auto-alternate direction for nested contours (holes)")
        self.outer_zone_only_ctrl = wx.CheckBox(panel, label="Outer zone only (largest polygon)")
        self.alternate_nesting_hatch_ctrl = wx.CheckBox(panel, label="Alternate nested contours (text mode)")
        self.invert_alternate_nesting_ctrl = wx.CheckBox(panel, label="Invert alternation (flip which nesting depth is hatched)")
        self.multi_angle_hatch_ctrl = wx.CheckBox(panel, label="Multi-angle hatch (overlay 0/45/90 deg)")
        self.use_pcbnew_geometry_ctrl = wx.CheckBox(
            panel, label="Use pcbnew-native geometry (experimental, merges touching same-net copper)"
        )

        def add_row(label: str, control: wx.Window):
            grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)

        add_row("Layer", self.layer_choice)
        add_row("Hatching mode", self.mode_choice)
        add_row("Hatch angle (deg)", self.angle_ctrl)
        add_row("Hatch spacing (mm)", self.spacing_ctrl)
        add_row("Manual spacing", self.manual_spacing_ctrl)
        add_row("Laser radius (mm)", self.radius_ctrl)
        add_row("Minimum hatch area (mm^2)", self.min_area_ctrl)
        add_row("Drill style", self.drill_style_choice)
        add_row("Drill contour start offset (mm)", self.drill_contour_start_ctrl)
        add_row("Drill contour spacing (mm)", self.drill_contour_spacing_ctrl)
        add_row("Drill contour count (perimeters)", self.drill_contour_count_ctrl)
        add_row("Drill spiral turns", self.spiral_turns_ctrl)
        add_row("Drill spiral inner ratio", self.spiral_inner_ratio_ctrl)
        add_row("Contour start offset (mm)", self.offset_start_ctrl)
        add_row("Contour spacing (mm)", self.offset_spacing_ctrl)
        add_row("Contour count", self.offset_count_ctrl)
        add_row("Contour direction", self.invert_offset_direction_ctrl)
        add_row("Auto-alternate nested direction", self.auto_alternate_direction_ctrl)
        add_row("Edge-cuts cleaning", self.outer_zone_only_ctrl)
        add_row("Nested text hatching", self.alternate_nesting_hatch_ctrl)
        add_row("Invert nested hatching", self.invert_alternate_nesting_ctrl)
        add_row("Multi-angle hatch", self.multi_angle_hatch_ctrl)
        add_row("Geometry source", self.use_pcbnew_geometry_ctrl)

        left.Add(grid, 0, wx.ALL | wx.EXPAND, 10)

        left.Add(wx.StaticText(panel, label="Zones"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.zone_list = wx.CheckListBox(panel)
        left.Add(self.zone_list, 1, wx.ALL | wx.EXPAND, 10)

        self.hatch_all_ctrl = wx.CheckBox(panel, label="Select all zones")
        self.hatch_all_ctrl.SetValue(True)
        left.Add(self.hatch_all_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.status_lbl = wx.StaticText(panel, label="")
        left.Add(self.status_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.export_btn = wx.Button(panel, label="Export DXF...")
        left.Add(self.export_btn, 0, wx.ALL | wx.EXPAND, 10)

        self.export_all_btn = wx.Button(panel, label="Export All Layers...")
        left.Add(self.export_all_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        settings_scroll.SetSizer(left)
        settings_scroll.FitInside()

        right = wx.BoxSizer(wx.VERTICAL)
        canvas_hint = wx.StaticText(
            outer_panel,
            label="Scroll to zoom, drag to pan, double-click or middle-click to reset view.",
        )
        right.Add(canvas_hint, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.canvas = PreviewCanvas(outer_panel)
        right.Add(self.canvas, 1, wx.ALL | wx.EXPAND, 6)

        root.Add(settings_scroll, 0, wx.EXPAND)
        root.Add(right, 1, wx.EXPAND)

        outer_panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(outer_panel, 1, wx.EXPAND)
        outer.Add(self.CreateSeparatedButtonSizer(wx.CLOSE), 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizerAndFit(outer)
        self.SetMinSize((1080, 680))

        self.layer_choice.Bind(wx.EVT_CHOICE, self._on_layer_changed)
        self.mode_choice.Bind(wx.EVT_CHOICE, self._on_settings_changed)
        self.manual_spacing_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.drill_style_choice.Bind(wx.EVT_CHOICE, self._on_settings_changed)
        self.invert_offset_direction_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.auto_alternate_direction_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.outer_zone_only_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.alternate_nesting_hatch_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.invert_alternate_nesting_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.multi_angle_hatch_ctrl.Bind(wx.EVT_CHECKBOX, self._on_settings_changed)
        self.use_pcbnew_geometry_ctrl.Bind(wx.EVT_CHECKBOX, self._on_geometry_source_changed)
        self.hatch_all_ctrl.Bind(wx.EVT_CHECKBOX, self._on_hatch_all)
        self.zone_list.Bind(wx.EVT_CHECKLISTBOX, self._on_zone_checked)
        for ctrl in (
            self.angle_ctrl,
            self.spacing_ctrl,
            self.radius_ctrl,
            self.min_area_ctrl,
            self.drill_contour_start_ctrl,
            self.drill_contour_spacing_ctrl,
            self.drill_contour_count_ctrl,
            self.spiral_turns_ctrl,
            self.spiral_inner_ratio_ctrl,
            self.offset_start_ctrl,
            self.offset_spacing_ctrl,
            self.offset_count_ctrl,
        ):
            ctrl.Bind(wx.EVT_TEXT, self._on_settings_changed)
        self.export_btn.Bind(wx.EVT_BUTTON, self._on_export)
        self.export_all_btn.Bind(wx.EVT_BUTTON, self._on_export_all)
        self.Bind(wx.EVT_CLOSE, self._on_close_window)

        if initial_layer in layer_choices:
            self.layer_choice.SetSelection(layer_choices.index(initial_layer))
        else:
            self.layer_choice.SetSelection(0)

        self._load_layer(self._current_layer())

    def _current_layer(self) -> str:
        return self.layer_choice.GetStringSelection() or self.layer_choices[0]

    def _apply_settings_to_controls(self, settings: dict[str, object]) -> None:
        self._suspend_events = True
        try:
            s = _sanitize_layer_settings(settings)

            mode_idx = self.mode_choice.FindString(str(s["mode"]))
            self.mode_choice.SetSelection(mode_idx if mode_idx != wx.NOT_FOUND else 0)

            self.angle_ctrl.SetValue(f"{float(s['angle']):.4f}".rstrip("0").rstrip("."))
            self.spacing_ctrl.SetValue(f"{float(s['spacing']):.4f}".rstrip("0").rstrip("."))
            self.manual_spacing_ctrl.SetValue(bool(s["useManualSpacing"]))
            self.radius_ctrl.SetValue(f"{float(s['laserRadius']):.4f}".rstrip("0").rstrip("."))
            self.min_area_ctrl.SetValue(f"{float(s['minArea']):.4f}".rstrip("0").rstrip("."))
            drill_style_idx = self.drill_style_choice.FindString(str(s["drillStyle"]))
            self.drill_style_choice.SetSelection(drill_style_idx if drill_style_idx != wx.NOT_FOUND else 0)
            self.drill_contour_start_ctrl.SetValue(f"{float(s['drillContourStart']):.4f}".rstrip("0").rstrip("."))
            self.drill_contour_spacing_ctrl.SetValue(f"{float(s['drillContourSpacing']):.4f}".rstrip("0").rstrip("."))
            self.drill_contour_count_ctrl.SetValue(str(int(s["drillContourCount"])))
            self.spiral_turns_ctrl.SetValue(f"{float(s['spiralTurns']):.4f}".rstrip("0").rstrip("."))
            self.spiral_inner_ratio_ctrl.SetValue(f"{float(s['spiralInnerRatio']):.4f}".rstrip("0").rstrip("."))
            self.offset_start_ctrl.SetValue(f"{float(s['offsetStart']):.4f}".rstrip("0").rstrip("."))
            self.offset_spacing_ctrl.SetValue(f"{float(s['offsetSpacing']):.4f}".rstrip("0").rstrip("."))
            self.offset_count_ctrl.SetValue(str(int(s["offsetCount"])))
            self.invert_offset_direction_ctrl.SetValue(bool(s["invertOffsetDirection"]))
            self.auto_alternate_direction_ctrl.SetValue(bool(s["autoAlternateContourDirection"]))
            self.hatch_all_ctrl.SetValue(bool(s["hatchAll"]))
            self.outer_zone_only_ctrl.SetValue(bool(s["outerZoneOnly"]))
            self.alternate_nesting_hatch_ctrl.SetValue(bool(s["alternateNestingHatch"]))
            self.invert_alternate_nesting_ctrl.SetValue(bool(s["invertAlternateNesting"]))
            self.multi_angle_hatch_ctrl.SetValue(bool(s["multiAngleHatch"]))
            self.use_pcbnew_geometry_ctrl.SetValue(bool(s["usePcbnewGeometry"]))
        finally:
            self._suspend_events = False

    def _read_controls_to_settings(self) -> tuple[dict[str, object] | None, str | None]:
        mode = self.mode_choice.GetStringSelection() or "contour_offsets"
        drill_style = self.drill_style_choice.GetStringSelection() or DEFAULT_DRILL_STYLE

        try:
            angle = float(self.angle_ctrl.GetValue())
            spacing = float(self.spacing_ctrl.GetValue())
            laser_radius = float(self.radius_ctrl.GetValue())
            min_area = float(self.min_area_ctrl.GetValue())
            drill_contour_start = float(self.drill_contour_start_ctrl.GetValue())
            drill_contour_spacing = float(self.drill_contour_spacing_ctrl.GetValue())
            drill_contour_count = int(self.drill_contour_count_ctrl.GetValue())
            spiral_turns = float(self.spiral_turns_ctrl.GetValue())
            spiral_inner_ratio = float(self.spiral_inner_ratio_ctrl.GetValue())
            offset_start = float(self.offset_start_ctrl.GetValue())
            offset_spacing = float(self.offset_spacing_ctrl.GetValue())
            offset_count = int(self.offset_count_ctrl.GetValue())
        except ValueError:
            return None, "Numeric settings are invalid."

        if mode in {"hatch", "contour_offsets"} and spacing <= 0:
            return None, "Hatch spacing must be greater than 0."
        if mode == "hatch" and laser_radius < 0:
            return None, "Laser radius must be >= 0."
        if mode == "hatch" and min_area < 0:
            return None, "Minimum hatch area must be >= 0."
        if mode == "drill" and drill_style == "spiral" and spiral_turns <= 0:
            return None, "Drill spiral turns must be greater than 0."
        if mode == "drill" and drill_style == "spiral" and not (0 <= spiral_inner_ratio < 1):
            return None, "Drill spiral inner ratio must be in [0, 1)."
        if mode == "drill" and drill_style == "contour" and (
            drill_contour_start < 0 or drill_contour_spacing < 0 or drill_contour_count <= 0
        ):
            return None, "Drill contour settings require non-negative values and count > 0."
        if mode == "contour_offsets" and (offset_start < 0 or offset_spacing < 0 or offset_count <= 0):
            return None, "Contour settings require non-negative values and count > 0."

        settings = {
            "mode": mode,
            "angle": angle,
            "spacing": spacing,
            "useManualSpacing": self.manual_spacing_ctrl.GetValue(),
            "laserRadius": laser_radius,
            "minArea": min_area,
            "drillStyle": drill_style,
            "drillContourStart": drill_contour_start,
            "drillContourSpacing": drill_contour_spacing,
            "drillContourCount": drill_contour_count,
            "spiralTurns": spiral_turns,
            "spiralInnerRatio": spiral_inner_ratio,
            "offsetStart": offset_start,
            "offsetSpacing": offset_spacing,
            "offsetCount": offset_count,
            "invertOffsetDirection": self.invert_offset_direction_ctrl.GetValue(),
            "autoAlternateContourDirection": self.auto_alternate_direction_ctrl.GetValue(),
            "hatchAll": self.hatch_all_ctrl.GetValue(),
            "outerZoneOnly": self.outer_zone_only_ctrl.GetValue(),
            "alternateNestingHatch": self.alternate_nesting_hatch_ctrl.GetValue(),
            "invertAlternateNesting": self.invert_alternate_nesting_ctrl.GetValue(),
            "multiAngleHatch": self.multi_angle_hatch_ctrl.GetValue(),
            "usePcbnewGeometry": self.use_pcbnew_geometry_ctrl.GetValue(),
        }
        return _sanitize_layer_settings(settings), None

    def _refresh_mode_control_states(self) -> None:
        mode = self.mode_choice.GetStringSelection() or "contour_offsets"
        is_contour = mode == "contour_offsets"
        is_drill = mode == "drill"
        drill_style = self.drill_style_choice.GetStringSelection() or DEFAULT_DRILL_STYLE
        is_drill_spiral = is_drill and drill_style == "spiral"
        is_drill_contour = is_drill and drill_style == "contour"
        manual = self.manual_spacing_ctrl.GetValue()

        self.angle_ctrl.Enable((not is_contour) and (not is_drill))
        self.manual_spacing_ctrl.Enable((not is_contour) and (not is_drill))
        self.spacing_ctrl.Enable(is_contour or ((not is_drill) and manual))
        self.radius_ctrl.Enable((not is_contour) and (not is_drill))
        self.min_area_ctrl.Enable((not is_contour) and (not is_drill))
        self.drill_style_choice.Enable(is_drill)
        self.drill_contour_start_ctrl.Enable(is_drill_contour)
        self.drill_contour_spacing_ctrl.Enable(is_drill_contour)
        self.drill_contour_count_ctrl.Enable(is_drill_contour)
        self.spiral_turns_ctrl.Enable(is_drill_spiral)
        self.spiral_inner_ratio_ctrl.Enable(is_drill_spiral)

        self.offset_start_ctrl.Enable(is_contour)
        self.offset_spacing_ctrl.Enable(is_contour)
        self.offset_count_ctrl.Enable(is_contour)
        self.invert_offset_direction_ctrl.Enable(is_contour)
        self.auto_alternate_direction_ctrl.Enable(is_contour)
        self.outer_zone_only_ctrl.Enable((not is_contour) and (not is_drill))
        self.alternate_nesting_hatch_ctrl.Enable((not is_contour) and (not is_drill) and (not self.outer_zone_only_ctrl.GetValue()))
        self.invert_alternate_nesting_ctrl.Enable(
            (not is_contour) and (not is_drill) and (not self.outer_zone_only_ctrl.GetValue())
            and self.alternate_nesting_hatch_ctrl.GetValue()
        )
        self.multi_angle_hatch_ctrl.Enable((not is_contour) and (not is_drill))
        self.use_pcbnew_geometry_ctrl.Enable(not is_drill)
        self.zone_list.Enable(not is_drill)
        self.hatch_all_ctrl.Enable(not is_drill)

    def _selected_ids(self) -> list[str]:
        ids: list[str] = []
        for i, zid in enumerate(self.zone_order):
            if i < self.zone_list.GetCount() and self.zone_list.IsChecked(i):
                ids.append(zid)
        return ids

    def _persist_current_settings(self) -> None:
        settings, err = self._read_controls_to_settings()
        if err or settings is None:
            return
        layer = self._current_layer()
        self._all_layer_settings[layer] = settings
        _save_all_layer_settings(self._all_layer_settings)
        _save_last_layer(layer)

    def _load_layer(self, layer: str) -> None:
        settings = self._all_layer_settings.get(layer, _default_layer_settings_for(layer))
        self._apply_settings_to_controls(settings)
        self._refresh_mode_control_states()

        mode = self.mode_choice.GetStringSelection() or "contour_offsets"
        if mode == "drill":
            self.session = None
            self.zone_map = {}
            self.zone_order = []
            self._pcbnew_ctx = None
            self.zone_list.Clear()
            self.status_lbl.SetLabel("Drill mode: reading drill holes from board data.")
            self._refresh_preview()
            return

        self.status_lbl.SetLabel(f"Exporting {layer} from KiCad...")
        wx.YieldIfNeeded()

        TEMP_DXF_DIR.mkdir(parents=True, exist_ok=True)
        raw_output_path = TEMP_DXF_DIR / f"{self.board_path.stem}-fiber-export-{uuid.uuid4().hex}.dxf"
        use_pcbnew = bool(settings.get("usePcbnewGeometry", False))
        self._pcbnew_ctx = None
        try:
            if use_pcbnew:
                net_polys, zone_net_map = _export_kicad_layer_via_pcbnew(layer, raw_output_path)
                self._pcbnew_ctx = {"net_polys": net_polys, "zone_net_map": zone_net_map}
            else:
                _run_kicad_dxf_export(self.kicad_cli, self.board_path, raw_output_path, layer)
        except Exception as exc:
            self.status_lbl.SetLabel(f"{'pcbnew' if use_pcbnew else 'DXF'} export failed: {exc}")
            self.session = None
            self.zone_map = {}
            self.zone_order = []
            self.zone_list.Clear()
            self.canvas.set_data({}, set(), [])
            return

        old_path = self.raw_output_path
        self.raw_output_path = raw_output_path
        if old_path is not None and old_path.exists():
            try:
                old_path.unlink()
            except Exception:
                pass

        try:
            session, zones = _build_local_session_from_dxf(raw_output_path)
        except Exception as exc:
            self.status_lbl.SetLabel(f"Failed to parse DXF: {exc}")
            self.session = None
            return

        self.session = session
        self.zone_map = {}
        self.zone_order = []
        labels: list[str] = []
        for z in zones:
            zid = str(z.get("id", "")).strip()
            pts = z.get("points") if isinstance(z.get("points"), list) else []
            if not zid or not pts:
                continue
            self.zone_order.append(zid)
            self.zone_map[zid] = pts
            area = float(z.get("area", 0.0)) if z.get("area") is not None else 0.0
            labels.append(f"#{zid}  area={area:.3f}")

        self._suspend_events = True
        try:
            self.zone_list.Set(labels)
            hatch_all = self.hatch_all_ctrl.GetValue()
            for i in range(self.zone_list.GetCount()):
                self.zone_list.Check(i, hatch_all)
        finally:
            self._suspend_events = False

        self._refresh_preview()

    def _on_layer_changed(self, _event) -> None:
        self._persist_current_settings()
        self.canvas.reset_view()
        self._load_layer(self._current_layer())

    def _on_settings_changed(self, _event) -> None:
        if self._suspend_events:
            return
        self._refresh_mode_control_states()
        self._refresh_preview()

    def _on_geometry_source_changed(self, _event) -> None:
        if self._suspend_events:
            return
        # Switching geometry source changes which polygons are exported for
        # this layer (net-merged pcbnew data vs. kicad-cli DXF), so the layer
        # needs a full reload, not just a preview refresh. Persist first so
        # _load_layer (which reads settings back from storage) picks up the
        # new checkbox value instead of the previously-saved one.
        self._refresh_mode_control_states()
        self._persist_current_settings()
        self._load_layer(self._current_layer())

    def _on_hatch_all(self, _event) -> None:
        if self._suspend_events:
            return
        checked = self.hatch_all_ctrl.GetValue()
        self._suspend_events = True
        try:
            for i in range(self.zone_list.GetCount()):
                self.zone_list.Check(i, checked)
        finally:
            self._suspend_events = False
        self._refresh_preview()

    def _on_zone_checked(self, _event) -> None:
        if self._suspend_events:
            return
        self._suspend_events = True
        try:
            self.hatch_all_ctrl.SetValue(False)
        finally:
            self._suspend_events = False
        self._refresh_preview()

    def _current_preview_payload(self) -> tuple[dict[str, object] | None, str | None]:
        settings, err = self._read_controls_to_settings()
        if err or settings is None:
            return None, err
        payload = {
            "selectedIds": self._selected_ids(),
            "boardPath": str(self.board_path),
            "mode": settings["mode"],
            "angle": settings["angle"],
            "spacing": settings["spacing"],
            "useManualSpacing": settings["useManualSpacing"],
            "laserRadius": settings["laserRadius"],
            "minArea": settings["minArea"],
            "drillStyle": settings["drillStyle"],
            "drillContourStart": settings["drillContourStart"],
            "drillContourSpacing": settings["drillContourSpacing"],
            "drillContourCount": settings["drillContourCount"],
            "spiralTurns": settings["spiralTurns"],
            "spiralInnerRatio": settings["spiralInnerRatio"],
            "outerZoneOnly": settings["outerZoneOnly"],
            "alternateNestingHatch": settings["alternateNestingHatch"],
            "invertAlternateNesting": settings["invertAlternateNesting"],
            "multiAngleHatch": settings["multiAngleHatch"],
            "offsetStart": settings["offsetStart"],
            "offsetSpacing": settings["offsetSpacing"],
            "offsetCount": settings["offsetCount"],
            "invertOffsetDirection": settings["invertOffsetDirection"],
            "autoAlternateContourDirection": settings["autoAlternateContourDirection"],
        }
        return payload, None

    def _refresh_preview(self) -> None:
        payload, err = self._current_preview_payload()
        if err or payload is None:
            self.status_lbl.SetLabel(err or "Invalid settings.")
            return

        mode = str(payload.get("mode", "hatch"))

        if mode == "drill":
            try:
                circles, segments = build_kicad_drill_geometry(
                    self.board_path,
                    style=str(payload.get("drillStyle", DEFAULT_DRILL_STYLE)),
                    spiral_turns=float(payload.get("spiralTurns", 1.75)),
                    spiral_inner_ratio=float(payload.get("spiralInnerRatio", 0.10)),
                    contour_start_offset=float(payload.get("drillContourStart", 0.05)),
                    contour_spacing=float(payload.get("drillContourSpacing", 0.05)),
                    contour_count=int(payload.get("drillContourCount", 4)),
                )
            except Exception as exc:
                self.status_lbl.SetLabel(f"Preview failed: {exc}")
                self.canvas.set_data({}, set(), [])
                return

            drill_zones: dict[str, list[list[float]]] = {}
            for idx, ring in enumerate(circles, start=1):
                drill_zones[str(idx)] = [[float(p[0]), float(p[1])] for p in ring]

            self.canvas.set_data(drill_zones, set(), segments)
            self.status_lbl.SetLabel(f"Drill holes: {len(drill_zones)}    Segments: {len(segments)}")
            return

        if self.session is None:
            return

        try:
            segments = _generate_preview_segments(
                self.session, payload, pcbnew_ctx=self._pcbnew_ctx, kicad_layer_name=self._current_layer()
            )
        except Exception as exc:
            self.status_lbl.SetLabel(f"Preview failed: {exc}")
            self.canvas.set_data(self.zone_map, set(self._selected_ids()), [])
            return
        self.canvas.set_data(self.zone_map, set(self._selected_ids()), segments)
        self.status_lbl.SetLabel(f"Zones: {len(self._selected_ids())}    Segments: {len(segments)}")

    def _on_export(self, _event) -> None:
        payload, err = self._current_preview_payload()
        if err or payload is None:
            _message("Fiber Laser Export", err or "Invalid settings.", wx.OK | wx.ICON_ERROR)
            return

        mode = str(payload.get("mode", "hatch"))
        if mode != "drill" and (self.session is None or self.raw_output_path is None):
            _message("Fiber Laser Export", "No preview data available to export.", wx.OK | wx.ICON_ERROR)
            return

        export_session = self.session
        if export_session is None:
            export_session = UploadSession(
                path=str(self.board_path),
                zone_map={},
                zone_payload=[],
                created_ts=time.time(),
                last_access_ts=time.time(),
                temp_paths=[],
            )

        try:
            body = _generate_export_dxf_bytes(
                export_session, payload, pcbnew_ctx=self._pcbnew_ctx, kicad_layer_name=self._current_layer()
            )
        except Exception as exc:
            _message("Fiber Laser Export", f"Export failed: {exc}", wx.OK | wx.ICON_ERROR)
            return

        layer = self._current_layer()
        mode = str(payload["mode"])
        output_default = self.raw_output_path.with_name(
            f"{self.board_path.stem}-{layer.replace('.', '_')}-{mode}.dxf"
        )
        with wx.FileDialog(
            self,
            "Save exported DXF",
            defaultDir=str(output_default.parent),
            defaultFile=output_default.name,
            wildcard="DXF files (*.dxf)|*.dxf",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as save_dlg:
            if save_dlg.ShowModal() != wx.ID_OK:
                return
            final_path = Path(save_dlg.GetPath())

        final_path.write_bytes(body)
        self.status_lbl.SetLabel(f"Export complete: {final_path}")

    def _on_export_all(self, _event) -> None:
        self._persist_current_settings()

        targets = _export_all_target_layers(self.layer_choices)
        if not targets:
            _message(
                "Fiber Laser Export All",
                "None of the standard layers (F.Cu, B.Cu, Edge.Cuts, F.Mask, B.Mask) were found on this board.",
                wx.OK | wx.ICON_WARNING,
            )
            return

        with wx.DirDialog(self, "Choose a folder for the exported DXF files") as dir_dlg:
            if dir_dlg.ShowModal() != wx.ID_OK:
                return
            out_dir = Path(dir_dlg.GetPath())

        self.status_lbl.SetLabel("Exporting all layers...")
        wx.YieldIfNeeded()

        config: dict[str, object] = {"board": str(self.board_path), "layers": {}}
        written: list[str] = []
        errors: list[str] = []

        for layer in targets:
            settings = self._all_layer_settings.get(layer, _default_layer_settings_for(layer))
            raw_path: Path | None = None
            try:
                session, zone_ids, raw_path, pcbnew_ctx = _build_session_for_kicad_layer(
                    self.kicad_cli, self.board_path, layer,
                    use_pcbnew=bool(settings.get("usePcbnewGeometry", False)),
                )
                payload = _payload_from_settings(settings, board_path=self.board_path, selected_ids=zone_ids)
                dxf_bytes = _generate_export_dxf_bytes(
                    session, payload, pcbnew_ctx=pcbnew_ctx, kicad_layer_name=layer
                )
            except Exception as exc:
                errors.append(f"{layer}: {exc}")
                continue
            finally:
                if raw_path is not None and raw_path.exists():
                    try:
                        raw_path.unlink()
                    except Exception:
                        pass

            safe_layer = layer.replace(".", "_").replace("/", "_")
            out_path = out_dir / f"{self.board_path.stem}_{safe_layer}.dxf"
            out_path.write_bytes(dxf_bytes)
            written.append(str(out_path))
            config["layers"][layer] = settings

        drill_settings = self._all_layer_settings.get(EXPORT_ALL_DRILL_LAYER, _default_layer_settings_for(EXPORT_ALL_DRILL_LAYER))
        drill_settings = dict(drill_settings)
        drill_settings["mode"] = "drill"
        try:
            drill_payload = _payload_from_settings(drill_settings, board_path=self.board_path, selected_ids=[])
            dxf_bytes = _generate_export_dxf_bytes(None, drill_payload)
            out_path = out_dir / f"{self.board_path.stem}_Drill.dxf"
            out_path.write_bytes(dxf_bytes)
            written.append(str(out_path))
            config["layers"][EXPORT_ALL_DRILL_LAYER] = drill_settings
        except Exception as exc:
            errors.append(f"Drill: {exc}")

        try:
            config_path = out_dir / f"{self.board_path.stem}_export_all_config.json"
            config_path.write_text(json.dumps(config, indent=2))
        except Exception:
            pass

        if errors:
            summary = f"Exported {len(written)} file(s) to {out_dir}.\n\nFailed:\n" + "\n".join(errors)
            _message("Fiber Laser Export All", summary, wx.OK | wx.ICON_WARNING)
        else:
            _message("Fiber Laser Export All", f"Exported {len(written)} file(s) to {out_dir}.", wx.OK | wx.ICON_INFORMATION)
        self.status_lbl.SetLabel(f"Export all complete: {len(written)} file(s) -> {out_dir}")

    def _on_close_window(self, _event) -> None:
        self._persist_current_settings()
        if self.raw_output_path is not None:
            try:
                self.raw_output_path.unlink(missing_ok=True)
            except Exception:
                pass
        self.Destroy()


class FiberLaserExportPlugin(pcbnew.ActionPlugin):
    @staticmethod
    def _icon_path() -> str:
        return str(PLUGIN_DIR / "icon_fiber_laser.xpm")

    def defaults(self) -> None:
        self.name = "Fiber Laser Launcher"
        self.category = "Fabrication"
        self.description = "Export board DXF with an in-window live preview"
        self.show_toolbar_button = True
        self.icon_file_name = self._icon_path()

    def GetShowToolbarButton(self, *args):  # pragma: no cover - KiCad callback
        return True

    def GetIconFileName(self, *args):  # pragma: no cover - KiCad callback
        return self._icon_path()

    def Run(self) -> None:
        board = pcbnew.GetBoard()
        if board is None:
            _message("Fiber Laser Launcher", "Open a PCB before running the exporter.", wx.OK | wx.ICON_ERROR)
            return

        board_path = Path(str(board.GetFileName()))
        if not board_path.name or not board_path.exists():
            _message("Fiber Laser Launcher", "Save the board first so KiCad can export it.", wx.OK | wx.ICON_ERROR)
            return

        kicad_cli = _find_kicad_cli()
        if kicad_cli is None:
            _message("Fiber Laser Launcher", "kicad-cli was not found on PATH.", wx.OK | wx.ICON_ERROR)
            return

        layer_choices = _extract_board_layer_names(board_path)
        if not layer_choices:
            _message("Fiber Laser Launcher", "No layers were found on the board.", wx.OK | wx.ICON_ERROR)
            return

        default_layer = _load_last_layer("F.Cu")
        if default_layer not in layer_choices:
            default_layer = layer_choices[0]

        parent_window = _find_kicad_parent_window()
        with FiberLaserWorkspaceDialog(parent_window, board_path, kicad_cli, layer_choices, default_layer) as dlg:
            dlg.ShowModal()
