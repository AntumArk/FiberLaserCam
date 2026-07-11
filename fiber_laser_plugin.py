from __future__ import annotations

import os
import re
import socket
import subprocess
import shutil
import tempfile
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

import pcbnew
import wx


PLUGIN_DIR = Path(__file__).resolve().parent


def _message(title: str, text: str, style: int) -> None:
    wx.MessageBox(text, title, style)


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


def _load_dependency_specs() -> list[str]:
    req_candidates = [
        Path(os.environ.get("FIBER_LASER_REQUIREMENTS_FILE", "").strip()) if os.environ.get("FIBER_LASER_REQUIREMENTS_FILE") else None,
        PLUGIN_DIR / "requirements.txt",
    ]

    names = {"numpy", "ezdxf", "shapely", "flask"}
    for req in req_candidates:
        if not req or not req.exists():
            continue
        picked: list[str] = []
        try:
            for raw in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                pkg = re.split(r"[<>=!~]", line, maxsplit=1)[0].strip().lower()
                if pkg in names:
                    picked.append(line)
        except Exception:
            continue

        if picked:
            if not any(re.split(r"[<>=!~]", s, maxsplit=1)[0].strip().lower() == "numpy" for s in picked):
                picked.append("numpy==1.26.4")
            if not any(re.split(r"[<>=!~]", s, maxsplit=1)[0].strip().lower() == "flask" for s in picked):
                picked.append("Flask==3.0.3")
            return picked

    return ["numpy==1.26.4", "ezdxf==1.4.4", "shapely==2.0.7", "Flask==3.0.3"]


def _ensure_web_runtime_python() -> str:
    web_venv = PLUGIN_DIR / ".webvenv"
    python_exe = web_venv / "bin" / "python"
    pip_exe = web_venv / "bin" / "pip"

    clean_env = dict(os.environ)
    clean_env.pop("PYTHONHOME", None)
    clean_env.pop("PYTHONPATH", None)

    if not python_exe.exists():
        create = subprocess.run(
            ["/usr/bin/python3", "-m", "venv", str(web_venv)],
            capture_output=True,
            text=True,
            env=clean_env,
        )
        if create.returncode != 0:
            details = create.stderr.strip() or create.stdout.strip() or "Unknown venv creation failure."
            raise RuntimeError(f"Failed to create web runtime venv:\n{details}")

    verify = subprocess.run(
        [str(python_exe), "-c", "import ezdxf, shapely, flask"],
        capture_output=True,
        text=True,
        env=clean_env,
    )
    if verify.returncode == 0:
        return str(python_exe)

    install = subprocess.run(
        [str(pip_exe), "install", "--upgrade", "pip", *_load_dependency_specs()],
        capture_output=True,
        text=True,
        env=clean_env,
    )
    if install.returncode != 0:
        details = install.stderr.strip() or install.stdout.strip() or "Unknown pip install failure."
        raise RuntimeError(f"Failed to install web runtime dependencies:\n{details}")

    verify = subprocess.run(
        [str(python_exe), "-c", "import ezdxf, shapely, flask"],
        capture_output=True,
        text=True,
        env=clean_env,
    )
    if verify.returncode != 0:
        details = verify.stderr.strip() or verify.stdout.strip() or "Unknown import verification failure."
        raise RuntimeError(f"Web runtime dependency verification failed:\n{details}")

    return str(python_exe)


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
    app_script = _find_web_app_script()
    if app_script is None:
        raise RuntimeError("Web app script not found. Put app.py in the bundle root.")

    python_exe = os.environ.get("FIBER_LASER_WEB_PYTHON", "").strip() or _ensure_web_runtime_python()
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}/"

    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["FIBER_LASER_EPHEMERAL"] = "1"
    env["FIBER_LASER_SERVER_TOKEN"] = token
    env["FIBER_LASER_WEB_HOST"] = "127.0.0.1"
    env["FIBER_LASER_WEB_PORT"] = str(port)
    env["FIBER_LASER_WEB_DEBUG"] = "0"

    log_path = Path(tempfile.gettempdir()) / f"fiberlaser_web_{token}.log"
    log_file = log_path.open("w", encoding="utf-8")
    subprocess.Popen(
        [python_exe, str(app_script)],
        cwd=str(app_script.parent),
        env=env,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    if not _wait_for_web_server(base_url, token, progress_dialog=progress_dialog):
        log_tail = _tail_log_file(log_path)
        details = f"Web server did not start at {base_url}"
        if log_tail:
            details += f"\n\nServer log tail:\n{log_tail}"
        raise RuntimeError(details)

    return base_url


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
        default_layer = os.environ.get("FIBER_LASER_EXPORT_LAYERS", "F.Cu")

        with wx.SingleChoiceDialog(
            None,
            "Choose layer to export for web processing:",
            "Fiber Laser Web Launcher",
            layer_choices,
        ) as layer_dlg:
            if default_layer in layer_choices:
                layer_dlg.SetSelection(layer_choices.index(default_layer))
            if layer_dlg.ShowModal() != wx.ID_OK:
                return
            selected_layer = layer_dlg.GetStringSelection()

        raw_output_path = PLUGIN_DIR / f"{board_path.stem}-fiber-web-{uuid.uuid4().hex}.dxf"
        try:
            _run_kicad_dxf_export(kicad_cli, board_path, raw_output_path, selected_layer)
        except RuntimeError as exc:
            _message("Fiber Laser Web Launcher", f"DXF export failed.\n\n{exc}", wx.OK | wx.ICON_ERROR)
            return

        token = uuid.uuid4().hex
        launch_dialog = wx.ProgressDialog(
            "Fiber Laser Web Launcher",
            "Starting local browser server...",
            maximum=100,
            parent=None,
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
            f"&mode={quote_plus('contour_offsets')}"
            f"&layers={quote_plus(selected_layer)}"
            f"&token={quote_plus(token)}"
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
