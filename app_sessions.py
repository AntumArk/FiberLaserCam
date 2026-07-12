from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UploadSession:
    path: str
    zone_map: dict[str, list[list[float]]]
    zone_payload: list[dict]
    created_ts: float
    last_access_ts: float
    temp_paths: list[str]


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
