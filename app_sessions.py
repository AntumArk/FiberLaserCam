from __future__ import annotations

import os
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EPHEMERAL_MODE = os.environ.get("FIBER_LASER_EPHEMERAL", "0") == "1"
SERVER_TOKEN = os.environ.get("FIBER_LASER_SERVER_TOKEN", "").strip()

STARTUP_IDLE_TIMEOUT_SEC = float(os.environ.get("FIBER_LASER_STARTUP_IDLE_TIMEOUT_SEC", "180"))
HEARTBEAT_IDLE_TIMEOUT_SEC = float(os.environ.get("FIBER_LASER_HEARTBEAT_IDLE_TIMEOUT_SEC", "20"))
SESSION_TTL_SEC = float(os.environ.get("FIBER_LASER_SESSION_TTL_SEC", "1800"))
JANITOR_INTERVAL_SEC = float(os.environ.get("FIBER_LASER_JANITOR_INTERVAL_SEC", "30"))
STALE_TEMP_FILE_TTL_SEC = float(os.environ.get("FIBER_LASER_STALE_TEMP_FILE_TTL_SEC", "3600"))

_START_TS = time.time()
_LAST_HEARTBEAT_TS = _START_TS
_HAS_HEARTBEAT = False
_DISCONNECT_REQUESTED = False
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_CALLBACK: Callable[[], None] | None = None
SESSION_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKERS_STARTED = False


@dataclass
class UploadSession:
    path: str
    zone_map: dict[str, list[list[float]]]
    zone_payload: list[dict]
    created_ts: float
    last_access_ts: float
    temp_paths: list[str]


SESSIONS: dict[str, UploadSession] = {}


def token_ok(token: str | None) -> bool:
    if not SERVER_TOKEN:
        return True
    return (token or "").strip() == SERVER_TOKEN


def touch_heartbeat() -> None:
    global _LAST_HEARTBEAT_TS, _HAS_HEARTBEAT
    with _SHUTDOWN_LOCK:
        _LAST_HEARTBEAT_TS = time.time()
        _HAS_HEARTBEAT = True


def configure_runtime(
    *,
    ephemeral_mode: bool | None = None,
    server_token: str | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> None:
    global EPHEMERAL_MODE, SERVER_TOKEN, _START_TS, _LAST_HEARTBEAT_TS, _HAS_HEARTBEAT, _DISCONNECT_REQUESTED, _SHUTDOWN_CALLBACK
    now = time.time()
    with _SHUTDOWN_LOCK:
        if ephemeral_mode is not None:
            EPHEMERAL_MODE = bool(ephemeral_mode)
        if server_token is not None:
            SERVER_TOKEN = str(server_token).strip()
        _START_TS = now
        _LAST_HEARTBEAT_TS = now
        _HAS_HEARTBEAT = False
        _DISCONNECT_REQUESTED = False
        _SHUTDOWN_CALLBACK = shutdown_callback


def _trigger_shutdown() -> None:
    callback: Callable[[], None] | None
    with _SHUTDOWN_LOCK:
        callback = _SHUTDOWN_CALLBACK

    if callback is not None:
        try:
            callback()
        except Exception:
            pass
        return

    os.kill(os.getpid(), signal.SIGTERM)


def _disarm_runtime_after_shutdown() -> None:
    global EPHEMERAL_MODE, _START_TS, _LAST_HEARTBEAT_TS, _HAS_HEARTBEAT, _DISCONNECT_REQUESTED, _SHUTDOWN_CALLBACK
    now = time.time()
    with _SHUTDOWN_LOCK:
        EPHEMERAL_MODE = False
        _START_TS = now
        _LAST_HEARTBEAT_TS = now
        _HAS_HEARTBEAT = False
        _DISCONNECT_REQUESTED = False
        _SHUTDOWN_CALLBACK = None


def request_disconnect() -> None:
    global _DISCONNECT_REQUESTED
    with _SHUTDOWN_LOCK:
        _DISCONNECT_REQUESTED = True


def safe_unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def create_session_record(
    dxf_path: str,
    zone_map: dict[str, list[list[float]]],
    zones: list[dict],
    temp_paths: list[str] | None = None,
) -> tuple[str, UploadSession]:
    now = time.time()
    upload_id = str(uuid.uuid4())
    session = UploadSession(
        path=dxf_path,
        zone_map=zone_map,
        zone_payload=zones,
        created_ts=now,
        last_access_ts=now,
        temp_paths=list(temp_paths or []),
    )
    return upload_id, session


def touch_session(upload_id: str) -> None:
    with SESSION_LOCK:
        session = SESSIONS.get(upload_id)
        if session is not None:
            session.last_access_ts = time.time()


def cleanup_session(upload_id: str, *, remove: bool = True) -> None:
    with SESSION_LOCK:
        session = SESSIONS.pop(upload_id, None) if remove else SESSIONS.get(upload_id)
    if session is None:
        return
    for temp_path in session.temp_paths:
        safe_unlink(temp_path)


def cleanup_all_sessions() -> None:
    with SESSION_LOCK:
        all_ids = list(SESSIONS.keys())
    for sid in all_ids:
        cleanup_session(sid, remove=True)


def cleanup_expired_sessions() -> None:
    cutoff = time.time() - SESSION_TTL_SEC
    expired_ids: list[str] = []
    with SESSION_LOCK:
        for upload_id, session in SESSIONS.items():
            if session.last_access_ts <= cutoff:
                expired_ids.append(upload_id)
    for upload_id in expired_ids:
        cleanup_session(upload_id, remove=True)


def cleanup_stale_temp_files(base_dir: Path) -> None:
    now = time.time()
    candidates: list[Path] = [Path(tempfile.gettempdir()), base_dir / "temp_dxf"]

    for base in candidates:
        if not base.exists() or not base.is_dir():
            continue
        for pattern in ("fiberlaser_upload_*.dxf", "*-fiber-web-*.dxf"):
            for entry in base.glob(pattern):
                try:
                    mtime = entry.stat().st_mtime
                except Exception:
                    continue
                if (now - mtime) > STALE_TEMP_FILE_TTL_SEC:
                    safe_unlink(str(entry))


def auto_shutdown_watchdog() -> None:
    while True:
        time.sleep(1.0)
        with _SHUTDOWN_LOCK:
            now = time.time()
            since_start = now - _START_TS
            since_heartbeat = now - _LAST_HEARTBEAT_TS
            ephemeral_mode = EPHEMERAL_MODE
            has_heartbeat = _HAS_HEARTBEAT
            disconnect_requested = _DISCONNECT_REQUESTED

        if not ephemeral_mode:
            continue

        if disconnect_requested:
            _trigger_shutdown()
            _disarm_runtime_after_shutdown()
            continue

        if not has_heartbeat and since_start > STARTUP_IDLE_TIMEOUT_SEC:
            _trigger_shutdown()
            _disarm_runtime_after_shutdown()
            continue

        if has_heartbeat and since_heartbeat > HEARTBEAT_IDLE_TIMEOUT_SEC:
            _trigger_shutdown()
            _disarm_runtime_after_shutdown()
            continue


def maintenance_janitor(base_dir: Path) -> None:
    while True:
        time.sleep(max(1.0, JANITOR_INTERVAL_SEC))
        cleanup_expired_sessions()
        cleanup_stale_temp_files(base_dir)


def start_background_workers(base_dir: Path) -> None:
    global _WORKERS_STARTED
    with _WORKER_LOCK:
        if _WORKERS_STARTED:
            return
        threading.Thread(target=auto_shutdown_watchdog, daemon=True).start()
        threading.Thread(target=maintenance_janitor, args=(base_dir,), daemon=True).start()
        _WORKERS_STARTED = True
