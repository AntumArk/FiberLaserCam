from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent


def _log_plugin_error(message: str) -> None:
    log_path = PLUGIN_DIR / "plugin_load.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass


plugin_dir_str = str(PLUGIN_DIR)
if plugin_dir_str not in sys.path:
    sys.path.insert(0, plugin_dir_str)

deps_dir_str = str(PLUGIN_DIR / ".deps")
if deps_dir_str not in sys.path:
    sys.path.insert(0, deps_dir_str)

try:
    from runtime_deps import extend_sys_path_for_deps
    extend_sys_path_for_deps(PLUGIN_DIR)
except Exception as exc:
    _log_plugin_error(f"runtime_deps import failed: {exc!r}")


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
