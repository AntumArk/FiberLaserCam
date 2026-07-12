from pathlib import Path
import sys

from runtime_deps import extend_sys_path_for_deps


def _log_plugin_error(message: str) -> None:
    log_path = Path.home() / ".local/share/kicad/10.0/scripting/plugins/fiberlasercam/plugin_load.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass


PLUGIN_DIR = Path(__file__).resolve().parent
plugin_dir_str = str(PLUGIN_DIR)
if plugin_dir_str not in sys.path:
    sys.path.insert(0, plugin_dir_str)

deps_dir_str = str(PLUGIN_DIR / ".deps")
if deps_dir_str not in sys.path:
    sys.path.insert(0, deps_dir_str)

extend_sys_path_for_deps(PLUGIN_DIR)


try:
    from .fiber_laser_plugin import FiberLaserExportPlugin
except Exception as exc:
    _log_plugin_error(f"relative import failed: {exc!r}")
    try:
        from fiber_laser_plugin import FiberLaserExportPlugin
    except Exception as exc2:
        _log_plugin_error(f"absolute import failed: {exc2!r}")
        FiberLaserExportPlugin = None

if FiberLaserExportPlugin is not None:
    try:
        FiberLaserExportPlugin().register()
        _log_plugin_error("plugin registered")
    except Exception as exc:
        _log_plugin_error(f"register failed: {exc!r}")
