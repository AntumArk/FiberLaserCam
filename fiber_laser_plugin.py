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

import ezdxf
from app_geometry import (
    DEFAULT_MIN_HATCH_AREA,
    build_contour_loops_for_selection,
    build_zone_payload_from_dxf_path,
    generate_contour_offsets_for_selection,
    generate_hatch_for_selection,
    resolve_spacing,
)
from app_sessions import UploadSession


PLUGIN_DIR = Path(__file__).resolve().parent
TEMP_DXF_DIR = PLUGIN_DIR / "temp_dxf"
SETTINGS_KEY = "layer_settings_json"
LAST_LAYER_KEY = "last_layer"


DEFAULT_LAYER_SETTINGS: dict[str, object] = {
    "mode": "contour_offsets",
    "action": "web_preview",
    "angle": 45.0,
    "spacing": 0.02,
    "useManualSpacing": True,
    "laserRadius": 0.01,
    "minArea": 0.30,
    "offsetStart": 0.02,
    "offsetSpacing": 0.02,
    "offsetCount": 3,
    "invertOffsetDirection": False,
    "hatchAll": True,
    "outerZoneOnly": False,
}


def _default_layer_settings_for(layer_name: str) -> dict[str, object]:
    base = dict(DEFAULT_LAYER_SETTINGS)
    if layer_name.strip().lower() == "edge.cuts":
        base["mode"] = "hatch"
        base["hatchAll"] = True
        base["outerZoneOnly"] = True
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
    if mode not in {"hatch", "contour_offsets"}:
        mode = str(DEFAULT_LAYER_SETTINGS["mode"])

    action = str(merged.get("action", DEFAULT_LAYER_SETTINGS["action"]))
    if action not in {"web_preview", "direct_export"}:
        action = str(DEFAULT_LAYER_SETTINGS["action"])

    clean: dict[str, object] = {
        "mode": mode,
        "action": action,
        "angle": _coerce_float(merged.get("angle"), float(DEFAULT_LAYER_SETTINGS["angle"])),
        "spacing": _coerce_float(merged.get("spacing"), float(DEFAULT_LAYER_SETTINGS["spacing"])),
        "useManualSpacing": _coerce_bool(merged.get("useManualSpacing"), bool(DEFAULT_LAYER_SETTINGS["useManualSpacing"])),
        "laserRadius": _coerce_float(merged.get("laserRadius"), float(DEFAULT_LAYER_SETTINGS["laserRadius"])),
        "minArea": _coerce_float(merged.get("minArea"), float(DEFAULT_LAYER_SETTINGS["minArea"])),
        "offsetStart": _coerce_float(merged.get("offsetStart"), float(DEFAULT_LAYER_SETTINGS["offsetStart"])),
        "offsetSpacing": _coerce_float(merged.get("offsetSpacing"), float(DEFAULT_LAYER_SETTINGS["offsetSpacing"])),
        "offsetCount": max(1, _coerce_int(merged.get("offsetCount"), int(DEFAULT_LAYER_SETTINGS["offsetCount"]))),
        "invertOffsetDirection": _coerce_bool(merged.get("invertOffsetDirection"), bool(DEFAULT_LAYER_SETTINGS["invertOffsetDirection"])),
        "hatchAll": _coerce_bool(merged.get("hatchAll"), bool(DEFAULT_LAYER_SETTINGS["hatchAll"])),
        "outerZoneOnly": _coerce_bool(merged.get("outerZoneOnly"), bool(DEFAULT_LAYER_SETTINGS["outerZoneOnly"])),
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


class LauncherSettingsDialog(wx.Dialog):
    def __init__(self, parent, layer_choices: list[str], initial_layer: str, initial_settings: dict[str, object]):
        super().__init__(parent, title="Fiber Laser Export Settings")
        self.layer_choices = layer_choices

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        self.layer_choice = wx.Choice(panel, choices=layer_choices)
        self.mode_choice = wx.Choice(panel, choices=["hatch", "contour_offsets"])
        self.action_choice = wx.Choice(panel, choices=["web_preview", "direct_export"])

        self.angle_ctrl = wx.TextCtrl(panel)
        self.spacing_ctrl = wx.TextCtrl(panel)
        self.manual_spacing_ctrl = wx.CheckBox(panel, label="Use manual hatch spacing")
        self.radius_ctrl = wx.TextCtrl(panel)
        self.min_area_ctrl = wx.TextCtrl(panel)
        self.offset_start_ctrl = wx.TextCtrl(panel)
        self.offset_spacing_ctrl = wx.TextCtrl(panel)
        self.offset_count_ctrl = wx.TextCtrl(panel)
        self.invert_offset_direction_ctrl = wx.CheckBox(panel, label="Invert offset direction (toward interior)")
        self.hatch_all_ctrl = wx.CheckBox(panel, label="Select all zones for export")
        self.outer_zone_only_ctrl = wx.CheckBox(panel, label="Outer zone only (largest polygon)")

        def add_row(label: str, control: wx.Window):
            grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)

        add_row("Layer", self.layer_choice)
        add_row("Hatching mode", self.mode_choice)
        add_row("Action", self.action_choice)
        add_row("Hatch angle (deg)", self.angle_ctrl)
        add_row("Hatch spacing (mm)", self.spacing_ctrl)
        add_row("Manual spacing", self.manual_spacing_ctrl)
        add_row("Laser radius (mm)", self.radius_ctrl)
        add_row("Minimum hatch area (mm^2)", self.min_area_ctrl)
        add_row("Contour start offset (mm)", self.offset_start_ctrl)
        add_row("Contour spacing (mm)", self.offset_spacing_ctrl)
        add_row("Contour count", self.offset_count_ctrl)
        add_row("Contour direction", self.invert_offset_direction_ctrl)
        add_row("Zone selection", self.hatch_all_ctrl)
        add_row("Edge-cuts cleaning", self.outer_zone_only_ctrl)

        helper = wx.StaticText(
            panel,
            label=(
                "Settings are saved per-layer in KiCad config. "
                "Direct export runs without opening preview."
            ),
        )
        helper.Wrap(560)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)

        root.Add(grid, 1, wx.ALL | wx.EXPAND, 12)
        root.Add(helper, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        if buttons:
            root.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((620, 520))

        if initial_layer in layer_choices:
            self.layer_choice.SetSelection(layer_choices.index(initial_layer))
        else:
            self.layer_choice.SetSelection(0)

        self._apply_settings_to_controls(initial_settings)
        self._refresh_mode_control_states()

        self.layer_choice.Bind(wx.EVT_CHOICE, self._on_layer_changed)
        self.mode_choice.Bind(wx.EVT_CHOICE, lambda evt: self._refresh_mode_control_states())
        self.manual_spacing_ctrl.Bind(wx.EVT_CHECKBOX, lambda evt: self._refresh_mode_control_states())

        self._all_layer_settings = _load_all_layer_settings()

    def _apply_settings_to_controls(self, settings: dict[str, object]) -> None:
        s = _sanitize_layer_settings(settings)

        mode_idx = self.mode_choice.FindString(str(s["mode"]))
        self.mode_choice.SetSelection(mode_idx if mode_idx != wx.NOT_FOUND else 0)

        action_idx = self.action_choice.FindString(str(s["action"]))
        self.action_choice.SetSelection(action_idx if action_idx != wx.NOT_FOUND else 0)

        self.angle_ctrl.SetValue(f"{float(s['angle']):.4f}".rstrip("0").rstrip("."))
        self.spacing_ctrl.SetValue(f"{float(s['spacing']):.4f}".rstrip("0").rstrip("."))
        self.manual_spacing_ctrl.SetValue(bool(s["useManualSpacing"]))
        self.radius_ctrl.SetValue(f"{float(s['laserRadius']):.4f}".rstrip("0").rstrip("."))
        self.min_area_ctrl.SetValue(f"{float(s['minArea']):.4f}".rstrip("0").rstrip("."))
        self.offset_start_ctrl.SetValue(f"{float(s['offsetStart']):.4f}".rstrip("0").rstrip("."))
        self.offset_spacing_ctrl.SetValue(f"{float(s['offsetSpacing']):.4f}".rstrip("0").rstrip("."))
        self.offset_count_ctrl.SetValue(str(int(s["offsetCount"])))
        self.invert_offset_direction_ctrl.SetValue(bool(s["invertOffsetDirection"]))
        self.hatch_all_ctrl.SetValue(bool(s["hatchAll"]))
        self.outer_zone_only_ctrl.SetValue(bool(s["outerZoneOnly"]))

    def _read_controls_to_settings(self) -> tuple[dict[str, object] | None, str | None]:
        mode = self.mode_choice.GetStringSelection() or "contour_offsets"
        action = self.action_choice.GetStringSelection() or "web_preview"

        try:
            angle = float(self.angle_ctrl.GetValue())
            spacing = float(self.spacing_ctrl.GetValue())
            laser_radius = float(self.radius_ctrl.GetValue())
            min_area = float(self.min_area_ctrl.GetValue())
            offset_start = float(self.offset_start_ctrl.GetValue())
            offset_spacing = float(self.offset_spacing_ctrl.GetValue())
            offset_count = int(self.offset_count_ctrl.GetValue())
        except ValueError:
            return None, "Numeric settings are invalid."

        if spacing <= 0:
            return None, "Hatch spacing must be greater than 0."
        if laser_radius < 0:
            return None, "Laser radius must be >= 0."
        if min_area < 0:
            return None, "Minimum hatch area must be >= 0."
        if offset_start < 0 or offset_spacing < 0 or offset_count <= 0:
            return None, "Contour settings require non-negative values and count > 0."

        settings = {
            "mode": mode,
            "action": action,
            "angle": angle,
            "spacing": spacing,
            "useManualSpacing": self.manual_spacing_ctrl.GetValue(),
            "laserRadius": laser_radius,
            "minArea": min_area,
            "offsetStart": offset_start,
            "offsetSpacing": offset_spacing,
            "offsetCount": offset_count,
            "invertOffsetDirection": self.invert_offset_direction_ctrl.GetValue(),
            "hatchAll": self.hatch_all_ctrl.GetValue(),
            "outerZoneOnly": self.outer_zone_only_ctrl.GetValue(),
        }
        return _sanitize_layer_settings(settings), None

    def _refresh_mode_control_states(self) -> None:
        mode = self.mode_choice.GetStringSelection() or "contour_offsets"
        is_contour = mode == "contour_offsets"
        manual = self.manual_spacing_ctrl.GetValue()

        self.angle_ctrl.Enable(not is_contour)
        self.manual_spacing_ctrl.Enable(not is_contour)
        self.spacing_ctrl.Enable(is_contour or manual)
        self.radius_ctrl.Enable(not is_contour)
        self.min_area_ctrl.Enable(not is_contour)

        self.offset_start_ctrl.Enable(is_contour)
        self.offset_spacing_ctrl.Enable(is_contour)
        self.offset_count_ctrl.Enable(is_contour)
        self.invert_offset_direction_ctrl.Enable(is_contour)
        self.outer_zone_only_ctrl.Enable(not is_contour)

    def _current_layer(self) -> str:
        return self.layer_choice.GetStringSelection() or self.layer_choices[0]

    def _on_layer_changed(self, event) -> None:
        layer = self._current_layer()
        s = self._all_layer_settings.get(layer, _default_layer_settings_for(layer))
        self._apply_settings_to_controls(s)
        self._refresh_mode_control_states()

    def collect_result(self) -> tuple[str, dict[str, object]] | None:
        layer = self._current_layer()
        settings, err = self._read_controls_to_settings()
        if err:
            _message("Fiber Laser Launcher", err, wx.OK | wx.ICON_ERROR)
            return None

        self._all_layer_settings[layer] = settings
        _save_all_layer_settings(self._all_layer_settings)
        _save_last_layer(layer)
        return layer, settings


class PreviewCanvas(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.zones: dict[str, list[list[float]]] = {}
        self.selected: set[str] = set()
        self.segments: list[list[list[float]]] = []
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def set_data(self, zones: dict[str, list[list[float]]], selected: set[str], segments: list[list[list[float]]]) -> None:
        self.zones = zones
        self.selected = selected
        self.segments = segments
        self.Refresh(False)

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

    def _on_paint(self, _event) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.Clear()

        bounds = self._collect_bounds()
        if bounds is None:
            return

        minx, miny, maxx, maxy = bounds
        width, height = self.GetClientSize()
        if width <= 2 or height <= 2:
            return

        span_x = max(maxx - minx, 1e-6)
        span_y = max(maxy - miny, 1e-6)
        margin = 16.0
        scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)

        def sx(x: float) -> int:
            return int(margin + ((x - minx) * scale))

        def sy(y: float) -> int:
            return int(height - (margin + ((y - miny) * scale)))

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


class NativePreviewDialog(wx.Dialog):
    def __init__(
        self,
        parent,
        session: UploadSession,
        zones: list[dict],
        selected_layer: str,
        settings: dict[str, object],
        raw_output_path: Path,
    ):
        super().__init__(parent, title="Fiber Laser Live Preview")
        self.session = session
        self.selected_layer = selected_layer
        self.settings = settings
        self.raw_output_path = raw_output_path

        self.zone_map: dict[str, list[list[float]]] = {}
        self.zone_order: list[str] = []
        for z in zones:
            zid = str(z.get("id", "")).strip()
            pts = z.get("points") if isinstance(z.get("points"), list) else []
            if not zid or not pts:
                continue
            self.zone_order.append(zid)
            self.zone_map[zid] = pts

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(panel, label="Zones"), 0, wx.ALL, 6)

        labels: list[str] = []
        for z in zones:
            zid = str(z.get("id", "")).strip()
            if not zid:
                continue
            area = float(z.get("area", 0.0)) if z.get("area") is not None else 0.0
            labels.append(f"#{zid}  area={area:.3f}")

        self.zone_list = wx.CheckListBox(panel, choices=labels)
        left.Add(self.zone_list, 1, wx.ALL | wx.EXPAND, 6)

        self.hatch_all_ctrl = wx.CheckBox(panel, label="Select all zones")
        self.hatch_all_ctrl.SetValue(bool(settings.get("hatchAll", True)))
        left.Add(self.hatch_all_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.outer_only_ctrl = wx.CheckBox(panel, label="Outer zone only")
        self.outer_only_ctrl.SetValue(bool(settings.get("outerZoneOnly", False)))
        self.outer_only_ctrl.Enable(str(settings.get("mode", "hatch")) == "hatch")
        left.Add(self.outer_only_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.refresh_btn = wx.Button(panel, label="Refresh Preview")
        left.Add(self.refresh_btn, 0, wx.ALL | wx.EXPAND, 6)

        self.export_btn = wx.Button(panel, label="Export DXF")
        left.Add(self.export_btn, 0, wx.ALL | wx.EXPAND, 6)

        self.status_lbl = wx.StaticText(panel, label="")
        left.Add(self.status_lbl, 0, wx.ALL | wx.EXPAND, 6)

        self.canvas = PreviewCanvas(panel)

        root.Add(left, 0, wx.EXPAND)
        root.Add(self.canvas, 1, wx.ALL | wx.EXPAND, 6)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        outer.Add(self.CreateSeparatedButtonSizer(wx.CLOSE), 0, wx.ALL | wx.EXPAND, 6)
        self.SetSizerAndFit(outer)
        self.SetMinSize((980, 640))

        self.zone_list.Bind(wx.EVT_CHECKLISTBOX, self._on_zone_checked)
        self.hatch_all_ctrl.Bind(wx.EVT_CHECKBOX, self._on_hatch_all)
        self.outer_only_ctrl.Bind(wx.EVT_CHECKBOX, self._on_refresh)
        self.refresh_btn.Bind(wx.EVT_BUTTON, self._on_refresh)
        self.export_btn.Bind(wx.EVT_BUTTON, self._on_export)

        if self.hatch_all_ctrl.GetValue():
            for i in range(self.zone_list.GetCount()):
                self.zone_list.Check(i, True)

        self._refresh_preview()

    def _selected_ids(self) -> list[str]:
        ids: list[str] = []
        for i, zid in enumerate(self.zone_order):
            if i < self.zone_list.GetCount() and self.zone_list.IsChecked(i):
                ids.append(zid)
        return ids

    def _preview_payload(self) -> dict[str, object]:
        return {
            "selectedIds": self._selected_ids(),
            "mode": self.settings["mode"],
            "angle": self.settings["angle"],
            "spacing": self.settings["spacing"],
            "useManualSpacing": self.settings["useManualSpacing"],
            "laserRadius": self.settings["laserRadius"],
            "minArea": self.settings["minArea"],
            "outerZoneOnly": self.outer_only_ctrl.GetValue(),
            "offsetStart": self.settings["offsetStart"],
            "offsetSpacing": self.settings["offsetSpacing"],
            "offsetCount": self.settings["offsetCount"],
            "invertOffsetDirection": self.settings["invertOffsetDirection"],
        }

    def _on_zone_checked(self, _event) -> None:
        self.hatch_all_ctrl.SetValue(False)
        self._refresh_preview()

    def _on_hatch_all(self, _event) -> None:
        checked = self.hatch_all_ctrl.GetValue()
        for i in range(self.zone_list.GetCount()):
            self.zone_list.Check(i, checked)
        self._refresh_preview()

    def _on_refresh(self, _event) -> None:
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        payload = self._preview_payload()
        try:
            segments = _generate_preview_segments(
                self.session,
                payload,
                outer_only_override=self.outer_only_ctrl.GetValue(),
            )
        except Exception as exc:
            self.status_lbl.SetLabel(f"Preview failed: {exc}")
            self.canvas.set_data(self.zone_map, set(self._selected_ids()), [])
            return
        self.canvas.set_data(self.zone_map, set(self._selected_ids()), segments)
        self.status_lbl.SetLabel(f"Zones: {len(self._selected_ids())}    Segments: {len(segments)}")

    def _on_export(self, _event) -> None:
        payload = self._preview_payload()
        try:
            body = _generate_export_dxf_bytes(
                self.session,
                payload,
                outer_only_override=self.outer_only_ctrl.GetValue(),
            )
        except Exception as exc:
            _message(
                "Fiber Laser Live Preview",
                f"Export failed: {exc}",
                wx.OK | wx.ICON_ERROR,
            )
            return

        output_default = self.raw_output_path.with_name(
            f"{self.raw_output_path.stem}-{self.selected_layer.replace('.', '_')}-{self.settings['mode']}.dxf"
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
        _message("Fiber Laser Live Preview", f"Export complete:\n{final_path}", wx.OK | wx.ICON_INFORMATION)


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


def _generate_preview_segments(
    session: UploadSession,
    payload: dict[str, object],
    *,
    outer_only_override: bool | None = None,
) -> list[list[list[float]]]:
    selected_ids = payload.get("selectedIds") or []
    mode = str(payload.get("mode", "hatch"))

    if mode == "contour_offsets":
        start_offset = float(payload.get("offsetStart", 0.2))
        offset_spacing = float(payload.get("offsetSpacing", 0.2))
        offset_count = int(payload.get("offsetCount", 3))
        invert_offset_direction = bool(payload.get("invertOffsetDirection", False))
        segments, _ = generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
            invert_offset_direction=invert_offset_direction,
        )
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
    if outer_only_override is not None:
        outer_zone_only = bool(outer_only_override)

    segments, _ = generate_hatch_for_selection(
        session,
        selected_ids,
        angle,
        spacing,
        laser_radius,
        min_area,
        outer_zone_only,
    )
    return segments


def _generate_export_dxf_bytes(
    session: UploadSession,
    payload: dict[str, object],
    *,
    outer_only_override: bool | None = None,
) -> bytes:
    selected_ids = payload.get("selectedIds") or []
    mode = str(payload.get("mode", "hatch"))

    if mode == "contour_offsets":
        start_offset = float(payload.get("offsetStart", 0.2))
        offset_spacing = float(payload.get("offsetSpacing", 0.2))
        offset_count = int(payload.get("offsetCount", 3))
        invert_offset_direction = bool(payload.get("invertOffsetDirection", False))
        segments, _ = generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
            invert_offset_direction=invert_offset_direction,
        )
        loops = build_contour_loops_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
            invert_offset_direction=invert_offset_direction,
        )
    else:
        segments = _generate_preview_segments(session, payload, outer_only_override=outer_only_override)
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
        for loop in loops:
            if len(loop) < 3:
                continue
            try:
                modelspace.add_lwpolyline(loop, close=True, dxfattribs={"layer": layer_name})
            except Exception:
                modelspace.add_polyline2d(loop, close=True, dxfattribs={"layer": layer_name})
    else:
        for seg in segments:
            p1, p2 = seg
            modelspace.add_line((p1[0], p1[1], 0.0), (p2[0], p2[1], 0.0), dxfattribs={"layer": layer_name})

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _open_native_preview_local(raw_output_path: Path, selected_layer: str, settings: dict[str, object], parent_window) -> None:
    progress = wx.ProgressDialog(
        "Fiber Laser Live Preview",
        "Preparing local preview...",
        maximum=100,
        parent=parent_window,
        style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_ELAPSED_TIME,
    )
    try:
        session, zones = _build_local_session_from_dxf(raw_output_path)
    finally:
        try:
            progress.Destroy()
        except Exception:
            pass

    dlg = NativePreviewDialog(
        parent_window,
        session=session,
        zones=zones,
        selected_layer=selected_layer,
        settings=settings,
        raw_output_path=raw_output_path,
    )
    dlg.ShowModal()


def _direct_export_local(raw_output_path: Path, selected_layer: str, settings: dict[str, object]) -> Path:
    session, zones = _build_local_session_from_dxf(raw_output_path)
    selected_ids = [str(z.get("id")) for z in zones if isinstance(z, dict) and z.get("id") is not None]
    if not selected_ids:
        raise RuntimeError("No closed zones were detected for direct export.")

    payload = {
        "selectedIds": selected_ids,
        "mode": settings["mode"],
        "angle": settings["angle"],
        "spacing": settings["spacing"],
        "useManualSpacing": settings["useManualSpacing"],
        "laserRadius": settings["laserRadius"],
        "minArea": settings["minArea"],
        "outerZoneOnly": settings["outerZoneOnly"],
        "offsetStart": settings["offsetStart"],
        "offsetSpacing": settings["offsetSpacing"],
        "offsetCount": settings["offsetCount"],
        "invertOffsetDirection": settings["invertOffsetDirection"],
    }
    body = _generate_export_dxf_bytes(session, payload)

    parent_window = _find_kicad_parent_window()
    output_default = raw_output_path.with_name(
        f"{raw_output_path.stem}-{selected_layer.replace('.', '_')}-{settings['mode']}.dxf"
    )
    with wx.FileDialog(
        parent_window,
        "Save direct export DXF",
        defaultDir=str(output_default.parent),
        defaultFile=output_default.name,
        wildcard="DXF files (*.dxf)|*.dxf",
        style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
    ) as save_dlg:
        if save_dlg.ShowModal() != wx.ID_OK:
            raise RuntimeError("Direct export cancelled.")
        final_path = Path(save_dlg.GetPath())

    final_path.write_bytes(body)
    return final_path


class FiberLaserExportPlugin(pcbnew.ActionPlugin):
    @staticmethod
    def _icon_path() -> str:
        return str(PLUGIN_DIR / "icon_fiber_laser.xpm")

    def defaults(self) -> None:
        self.name = "Fiber Laser Launcher"
        self.category = "Fabrication"
        self.description = "Export board DXF with native preview and direct export"
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
        default_layer = _load_last_layer("F.Cu")
        if default_layer not in layer_choices and layer_choices:
            default_layer = layer_choices[0]

        all_settings = _load_all_layer_settings()
        initial_settings = all_settings.get(default_layer, _default_layer_settings_for(default_layer))
        parent_window = _find_kicad_parent_window()
        with LauncherSettingsDialog(parent_window, layer_choices, default_layer, initial_settings) as settings_dlg:
            if settings_dlg.ShowModal() != wx.ID_OK:
                return
            collected = settings_dlg.collect_result()
            if collected is None:
                return
            selected_layer, layer_settings = collected

        TEMP_DXF_DIR.mkdir(parents=True, exist_ok=True)
        raw_output_path = TEMP_DXF_DIR / f"{board_path.stem}-fiber-export-{uuid.uuid4().hex}.dxf"
        try:
            _run_kicad_dxf_export(kicad_cli, board_path, raw_output_path, selected_layer)
        except RuntimeError as exc:
            _message("Fiber Laser Launcher", f"DXF export failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
            return

        if str(layer_settings["action"]) == "direct_export":
            try:
                out_path = _direct_export_local(raw_output_path, selected_layer, layer_settings)
            except RuntimeError as exc:
                if "cancelled" not in str(exc).lower():
                    _message("Fiber Laser Launcher", f"Direct export failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
                return
            _message(
                "Fiber Laser Launcher",
                (
                    "Direct export complete.\n\n"
                    f"Layer: {selected_layer}\n"
                    f"Mode: {layer_settings['mode']}\n"
                    f"Output: {out_path}"
                ),
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        try:
            _open_native_preview_local(raw_output_path, selected_layer, layer_settings, parent_window)
        except Exception as exc:
            _message("Fiber Laser Launcher", f"Live preview failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
            return
