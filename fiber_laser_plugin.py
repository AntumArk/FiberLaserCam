from __future__ import annotations

import os
import json
import importlib
import re
import socket
import subprocess
import shutil
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

import pcbnew
import wx


PLUGIN_DIR = Path(__file__).resolve().parent
TEMP_DXF_DIR = PLUGIN_DIR / "temp_dxf"
SETTINGS_KEY = "layer_settings_json"
LAST_LAYER_KEY = "last_layer"
_ACTIVE_EMBEDDED_SERVER = None


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


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _find_web_app_script() -> Path | None:
    env_path = os.environ.get("FIBER_LASER_WEB_APP", "").strip()
    candidates = [
        Path(env_path) if env_path else None,
        PLUGIN_DIR / "app.py",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def _python_can_import_web_deps(python_exe: str) -> bool:
    if not python_exe:
        return False

    clean_env = dict(os.environ)
    clean_env.pop("PYTHONHOME", None)
    clean_env.pop("PYTHONPATH", None)

    deps_dir = str(PLUGIN_DIR / ".deps")
    code = f"import sys; sys.path.insert(0, {deps_dir!r}); import ezdxf, shapely, flask"
    check = subprocess.run(
        [python_exe, "-c", code],
        capture_output=True,
        text=True,
        env=clean_env,
    )
    return check.returncode == 0


def _runtime_python_candidates() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(candidate: str | None) -> None:
        if not candidate:
            return
        resolved = str(Path(candidate).expanduser())
        if resolved in seen:
            return
        if not Path(resolved).exists():
            return
        seen.add(resolved)
        candidates.append(resolved)

    add_candidate(sys.executable)
    add_candidate(shutil.which("kicad-python"))
    add_candidate(shutil.which("python3"))
    add_candidate(shutil.which("python"))

    if sys.platform.startswith("linux"):
        appdir = os.environ.get("APPDIR", "").strip()
        if appdir:
            add_candidate(str(Path(appdir) / "usr/bin/python3"))
            add_candidate(str(Path(appdir) / "usr/bin/python"))
        add_candidate("/usr/bin/kicad-python")
        add_candidate("/usr/lib/kicad/python/bin/python3")
        add_candidate("/usr/lib64/kicad/python/bin/python3")

    return candidates


def _detect_web_runtime_python() -> str | None:
    for candidate in _runtime_python_candidates():
        if _python_can_import_web_deps(candidate):
            return candidate

    return None


def _ensure_web_runtime_python() -> str:
    detected = _detect_web_runtime_python()
    if detected:
        return detected

    tried = "\n".join(f"- {candidate}" for candidate in _runtime_python_candidates())
    raise RuntimeError(
        "Failed to prepare a web runtime. No usable Python interpreter could import Flask, ezdxf, and shapely from the plugin bundle.\n\n"
        "Tried interpreters:\n"
        f"{tried}\n\n"
        "This plugin expects bundled dependencies in the plugin-local .deps folder. Reinstall from a release package that includes .deps, "
        "or set FIBER_LASER_WEB_PYTHON to a compatible interpreter that can import the bundled dependencies."
    )


def _tail_log_file(log_path: Path, lines: int = 40) -> str:
    try:
        content = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    if not content:
        return ""
    return "\n".join(content[-lines:])


def _wait_for_web_server(base_url: str, token: str, timeout_sec: float = 30.0, progress_dialog=None) -> bool:
    deadline = time.time() + timeout_sec
    ping_url = f"{base_url}api/ping?token={quote_plus(token)}"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(ping_url, timeout=1.0) as resp:  # nosec B310
                if resp.status == 200:
                    return True
        except Exception:
            if progress_dialog is not None:
                try:
                    progress_dialog.Pulse(f"Starting local browser server... {base_url}")
                    wx.YieldIfNeeded()
                except Exception:
                    pass
            time.sleep(0.25)
    return False


def _start_web_server_for_session(token: str, progress_dialog=None) -> str:
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}/"

    global _ACTIVE_EMBEDDED_SERVER
    if _ACTIVE_EMBEDDED_SERVER is not None:
        try:
            _ACTIVE_EMBEDDED_SERVER.shutdown()
        except Exception:
            pass
        _ACTIVE_EMBEDDED_SERVER = None

    try:
        app_module = importlib.import_module("app")
    except Exception as exc:
        raise RuntimeError(f"Failed to import embedded web app from KiCad Python: {exc}") from exc

    if not hasattr(app_module, "start_embedded_server"):
        raise RuntimeError("Embedded web app helper is missing from app.py.")

    _ACTIVE_EMBEDDED_SERVER = app_module.start_embedded_server("127.0.0.1", port, token)

    if not _wait_for_web_server(base_url, token, progress_dialog=progress_dialog):
        details = f"Web server did not start at {base_url}"
        try:
            if _ACTIVE_EMBEDDED_SERVER is not None:
                _ACTIVE_EMBEDDED_SERVER.shutdown()
        except Exception:
            pass
        _ACTIVE_EMBEDDED_SERVER = None
        raise RuntimeError(details)

    return base_url


def _http_post_json(url: str, payload: dict[str, object], timeout: float = 30.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read()
            body = raw.decode("utf-8", errors="replace") if raw else ""
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        body = raw.decode("utf-8", errors="replace") if raw else str(exc)
        return int(exc.code), body


def _http_post_raw(url: str, payload: dict[str, object], timeout: float = 30.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        return int(exc.code), raw


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
            _message("Fiber Laser Web Launcher", err, wx.OK | wx.ICON_ERROR)
            return None

        self._all_layer_settings[layer] = settings
        _save_all_layer_settings(self._all_layer_settings)
        _save_last_layer(layer)
        return layer, settings


def _start_web_session_and_upload(raw_output_path: Path, token: str, progress_dialog=None):
    base_url = _start_web_server_for_session(token, progress_dialog=progress_dialog)
    status, body = _http_post_json(f"{base_url}api/upload-path", {"path": str(raw_output_path)})
    if status != 200:
        raise RuntimeError(f"Failed to create upload session: HTTP {status}\n{body}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid upload response: {exc}") from exc

    upload_id = str(payload.get("uploadId", "")).strip()
    zones = payload.get("zones") if isinstance(payload.get("zones"), list) else []
    if not upload_id:
        raise RuntimeError("Upload session response is missing uploadId.")
    return base_url, upload_id, zones


def _disconnect_web_session(base_url: str, token: str) -> None:
    global _ACTIVE_EMBEDDED_SERVER
    try:
        _http_post_json(f"{base_url}api/disconnect", {"token": token}, timeout=5.0)
    except Exception:
        pass
    if _ACTIVE_EMBEDDED_SERVER is not None:
        try:
            _ACTIVE_EMBEDDED_SERVER.shutdown()
        except Exception:
            pass
        _ACTIVE_EMBEDDED_SERVER = None


def _direct_export_via_web(raw_output_path: Path, selected_layer: str, settings: dict[str, object]) -> Path:
    token = uuid.uuid4().hex
    parent_window = _find_kicad_parent_window()
    progress = wx.ProgressDialog(
        "Fiber Laser Web Launcher",
        "Starting local export server...",
        maximum=100,
        parent=parent_window,
        style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_ELAPSED_TIME,
    )
    base_url = ""
    try:
        base_url, upload_id, zones = _start_web_session_and_upload(raw_output_path, token, progress_dialog=progress)
        selected_ids = [str(z.get("id")) for z in zones if isinstance(z, dict) and z.get("id") is not None]
        if not selected_ids:
            raise RuntimeError("No closed zones were detected for direct export.")

        payload = {
            "uploadId": upload_id,
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
        status, body = _http_post_raw(f"{base_url}api/export", payload, timeout=120.0)
        if status != 200:
            raise RuntimeError(f"Direct export failed: HTTP {status}\n{body.decode('utf-8', errors='replace')}")

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
    finally:
        if base_url:
            _disconnect_web_session(base_url, token)
        try:
            progress.Destroy()
        except Exception:
            pass


class FiberLaserExportPlugin(pcbnew.ActionPlugin):
    @staticmethod
    def _icon_path() -> str:
        return str(PLUGIN_DIR / "icon_fiber_laser.xpm")

    def defaults(self) -> None:
        self.name = "Fiber Laser Web Launcher"
        self.category = "Fabrication"
        self.description = "Export board DXF and open browser workflow"
        self.show_toolbar_button = True
        self.icon_file_name = self._icon_path()

    def GetShowToolbarButton(self, *args):  # pragma: no cover - KiCad callback
        return True

    def GetIconFileName(self, *args):  # pragma: no cover - KiCad callback
        return self._icon_path()

    def Run(self) -> None:
        board = pcbnew.GetBoard()
        if board is None:
            _message("Fiber Laser Web Launcher", "Open a PCB before running the exporter.", wx.OK | wx.ICON_ERROR)
            return

        board_path = Path(str(board.GetFileName()))
        if not board_path.name or not board_path.exists():
            _message("Fiber Laser Web Launcher", "Save the board first so KiCad can export it.", wx.OK | wx.ICON_ERROR)
            return

        kicad_cli = _find_kicad_cli()
        if kicad_cli is None:
            _message("Fiber Laser Web Launcher", "kicad-cli was not found on PATH.", wx.OK | wx.ICON_ERROR)
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
        raw_output_path = TEMP_DXF_DIR / f"{board_path.stem}-fiber-web-{uuid.uuid4().hex}.dxf"
        try:
            _run_kicad_dxf_export(kicad_cli, board_path, raw_output_path, selected_layer)
        except RuntimeError as exc:
            _message("Fiber Laser Web Launcher", f"DXF export failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
            return

        if str(layer_settings["action"]) == "direct_export":
            try:
                out_path = _direct_export_via_web(raw_output_path, selected_layer, layer_settings)
            except RuntimeError as exc:
                if "cancelled" not in str(exc).lower():
                    _message("Fiber Laser Web Launcher", f"Direct export failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
                return
            _message(
                "Fiber Laser Web Launcher",
                (
                    "Direct export complete.\n\n"
                    f"Layer: {selected_layer}\n"
                    f"Mode: {layer_settings['mode']}\n"
                    f"Output: {out_path}"
                ),
                wx.OK | wx.ICON_INFORMATION,
            )
            return

        token = uuid.uuid4().hex
        launch_dialog = wx.ProgressDialog(
            "Fiber Laser Web Launcher",
            "Starting local browser server...",
            maximum=100,
            parent=parent_window,
            style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_ELAPSED_TIME,
        )
        try:
            base_url = _start_web_server_for_session(token, progress_dialog=launch_dialog)
        except Exception as exc:
            try:
                launch_dialog.Destroy()
            except Exception:
                pass
            _message("Fiber Laser Web Launcher", f"Failed to start web server.\n\n{exc}", wx.OK | wx.ICON_ERROR)
            return
        finally:
            try:
                launch_dialog.Destroy()
            except Exception:
                pass

        web_url = (
            f"{base_url}?sourcePath={quote_plus(str(raw_output_path))}"
            f"&mode={quote_plus(str(layer_settings['mode']))}"
            f"&layers={quote_plus(selected_layer)}"
            f"&token={quote_plus(token)}"
            f"&layer={quote_plus(selected_layer)}"
            f"&angle={quote_plus(str(layer_settings['angle']))}"
            f"&spacing={quote_plus(str(layer_settings['spacing']))}"
            f"&useManualSpacing={quote_plus('1' if bool(layer_settings['useManualSpacing']) else '0')}"
            f"&laserRadius={quote_plus(str(layer_settings['laserRadius']))}"
            f"&minArea={quote_plus(str(layer_settings['minArea']))}"
            f"&outerZoneOnly={quote_plus('1' if bool(layer_settings['outerZoneOnly']) else '0')}"
            f"&offsetStart={quote_plus(str(layer_settings['offsetStart']))}"
            f"&offsetSpacing={quote_plus(str(layer_settings['offsetSpacing']))}"
            f"&offsetCount={quote_plus(str(layer_settings['offsetCount']))}"
            f"&invertOffsetDirection={quote_plus('1' if bool(layer_settings['invertOffsetDirection']) else '0')}"
            f"&hatchAll={quote_plus('1' if bool(layer_settings['hatchAll']) else '0')}"
        )
        webbrowser.open(web_url)

        _message(
            "Fiber Laser Web Launcher",
            (
                "Opened browser workflow.\n\n"
                f"Layer: {selected_layer}\n"
                f"Raw DXF: {raw_output_path}\n\n"
                "Closing the browser tab will stop this temporary server automatically."
            ),
            wx.OK | wx.ICON_INFORMATION,
        )
