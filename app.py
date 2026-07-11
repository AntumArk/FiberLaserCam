from __future__ import annotations

import io
import os
import tempfile

import ezdxf
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from app_geometry import (
    DEFAULT_MIN_HATCH_AREA,
    DEFAULT_MODE,
    build_contour_loops_for_selection,
    build_zone_payload_from_dxf_path,
    generate_contour_offsets_for_selection,
    generate_hatch_for_selection,
    resolve_spacing,
)
from app_sessions import (
    EPHEMERAL_MODE,
    SESSIONS,
    SESSION_LOCK,
    cleanup_all_sessions,
    cleanup_session,
    create_session_record,
    request_disconnect,
    start_background_workers,
    token_ok,
    touch_heartbeat,
    touch_session,
)

app = Flask(__name__)
start_background_workers(base_dir=os.path.dirname(__file__))


@app.errorhandler(Exception)
def handle_api_exception(exc):
    if request.path.startswith("/api/"):
        if isinstance(exc, HTTPException):
            return jsonify({"error": exc.description}), exc.code
        return jsonify({"error": str(exc) or "Internal server error"}), 500

    if isinstance(exc, HTTPException):
        return exc
    return "Internal server error", 500


def create_upload_session_from_dxf_path(dxf_path: str, temp_paths: list[str] | None = None) -> tuple[str, list[dict]]:
    zones, zone_map = build_zone_payload_from_dxf_path(dxf_path)
    upload_id, session = create_session_record(dxf_path, zone_map, zones, temp_paths=temp_paths)
    with SESSION_LOCK:
        SESSIONS[upload_id] = session
    return upload_id, zones


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/ping")
def ping():
    token = request.args.get("token", "")
    if EPHEMERAL_MODE and not token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    if token:
        touch_heartbeat()
    return jsonify({"ok": True})


@app.post("/api/heartbeat")
def heartbeat():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "") or request.args.get("token", ""))
    if EPHEMERAL_MODE and not token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    touch_heartbeat()
    return jsonify({"ok": True})


@app.post("/api/disconnect")
def disconnect():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "") or request.args.get("token", ""))
    if EPHEMERAL_MODE and not token_ok(token):
        return jsonify({"error": "Invalid token."}), 403
    cleanup_all_sessions()
    request_disconnect()
    return jsonify({"ok": True})


@app.post("/api/upload")
def upload_file():
    file = request.files.get("file")
    if file is None or file.filename == "":
        return jsonify({"error": "No file uploaded."}), 400

    _, ext = os.path.splitext(file.filename.lower())
    if ext != ".dxf":
        return jsonify({"error": "Only DXF files are supported."}), 400

    data = file.read()
    if not data:
        return jsonify({"error": "Uploaded file is empty."}), 400

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".dxf", prefix="fiberlaser_upload_")
    temp.write(data)
    temp.flush()
    temp.close()

    try:
        upload_id, zones = create_upload_session_from_dxf_path(temp.name, temp_paths=[temp.name])
    except Exception as exc:  # noqa: BLE001
        os.unlink(temp.name)
        return jsonify({"error": f"Failed to parse DXF: {exc}"}), 400

    return jsonify({"uploadId": upload_id, "zones": zones})


@app.post("/api/upload-path")
def upload_path():
    payload = request.get_json(silent=True) or {}
    raw_path = str(payload.get("path", "")).strip()
    if not raw_path:
        return jsonify({"error": "DXF path is required."}), 400

    dxf_path = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.isfile(dxf_path):
        return jsonify({"error": f"DXF file not found: {dxf_path}"}), 404

    _, ext = os.path.splitext(dxf_path.lower())
    if ext != ".dxf":
        return jsonify({"error": "Only DXF files are supported."}), 400

    try:
        upload_id, zones = create_upload_session_from_dxf_path(dxf_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to parse DXF: {exc}"}), 400

    return jsonify({"uploadId": upload_id, "zones": zones})


@app.get("/api/session/<upload_id>")
def get_session(upload_id: str):
    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    touch_session(upload_id)
    return jsonify({"uploadId": upload_id, "zones": session.zone_payload})


@app.post("/api/preview")
def preview_hatch():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("uploadId")
    selected_ids = payload.get("selectedIds") or []

    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    touch_session(upload_id)
    mode = str(payload.get("mode", DEFAULT_MODE))
    if mode not in {"hatch", "contour_offsets"}:
        return jsonify({"error": "Invalid mode."}), 400

    if mode == "contour_offsets":
        try:
            start_offset = float(payload.get("offsetStart", 0.2))
            offset_spacing = float(payload.get("offsetSpacing", 0.2))
            offset_count = int(payload.get("offsetCount", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid contour offset values."}), 400

        if start_offset < 0 or offset_spacing < 0 or offset_count <= 0:
            return jsonify({"error": "Contour offset values must be non-negative, with count > 0."}), 400

        segments, stats = generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
        )
        return jsonify({"segments": segments, "effectiveSpacing": offset_spacing, "stats": stats})

    try:
        angle = float(payload.get("angle", 45))
        laser_radius = float(payload.get("laserRadius", 0.01))
        min_area = float(payload.get("minArea", DEFAULT_MIN_HATCH_AREA))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric control values."}), 400

    if min_area < 0:
        return jsonify({"error": "Minimum area must be >= 0."}), 400

    use_manual_spacing = bool(payload.get("useManualSpacing", False))
    outer_zone_only = bool(payload.get("outerZoneOnly", False))
    spacing_value: float | None = None
    spacing_raw = payload.get("spacing", None)
    if spacing_raw not in (None, ""):
        try:
            spacing_value = float(spacing_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid spacing value."}), 400

    spacing, spacing_error = resolve_spacing(use_manual_spacing, spacing_value, laser_radius)
    if spacing_error is not None or spacing is None:
        return jsonify({"error": spacing_error}), 400

    segments, stats = generate_hatch_for_selection(
        session,
        selected_ids,
        angle,
        spacing,
        laser_radius,
        min_area,
        outer_zone_only,
    )

    return jsonify({"segments": segments, "effectiveSpacing": spacing, "stats": stats})


@app.post("/api/export")
def export_dxf():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("uploadId")
    selected_ids = payload.get("selectedIds") or []

    session = SESSIONS.get(upload_id)
    if session is None:
        return jsonify({"error": "Upload session not found. Re-upload DXF."}), 404

    touch_session(upload_id)
    mode = str(payload.get("mode", DEFAULT_MODE))
    if mode not in {"hatch", "contour_offsets"}:
        return jsonify({"error": "Invalid mode."}), 400

    if mode == "contour_offsets":
        try:
            start_offset = float(payload.get("offsetStart", 0.2))
            offset_spacing = float(payload.get("offsetSpacing", 0.2))
            offset_count = int(payload.get("offsetCount", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid contour offset values."}), 400

        if start_offset < 0 or offset_spacing < 0 or offset_count <= 0:
            return jsonify({"error": "Contour offset values must be non-negative, with count > 0."}), 400

        segments, _ = generate_contour_offsets_for_selection(
            session,
            selected_ids,
            start_offset,
            offset_spacing,
            offset_count,
        )
        loops = build_contour_loops_for_selection(session, selected_ids, start_offset, offset_spacing, offset_count)
    else:
        try:
            angle = float(payload.get("angle", 45))
            laser_radius = float(payload.get("laserRadius", 0.01))
            min_area = float(payload.get("minArea", DEFAULT_MIN_HATCH_AREA))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid numeric control values."}), 400

        if min_area < 0:
            return jsonify({"error": "Minimum area must be >= 0."}), 400

        use_manual_spacing = bool(payload.get("useManualSpacing", False))
        outer_zone_only = bool(payload.get("outerZoneOnly", False))
        spacing_value: float | None = None
        spacing_raw = payload.get("spacing", None)
        if spacing_raw not in (None, ""):
            try:
                spacing_value = float(spacing_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid spacing value."}), 400

        spacing, spacing_error = resolve_spacing(use_manual_spacing, spacing_value, laser_radius)
        if spacing_error is not None or spacing is None:
            return jsonify({"error": spacing_error}), 400

        segments, _ = generate_hatch_for_selection(
            session,
            selected_ids,
            angle,
            spacing,
            laser_radius,
            min_area,
            outer_zone_only,
        )
        loops = []

    try:
        source_doc = ezdxf.readfile(session.path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to re-open DXF: {exc}"}), 500

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
    out = io.BytesIO(stream.getvalue().encode("utf-8"))
    out.seek(0)

    cleanup_session(str(upload_id), remove=True)

    return send_file(
        out,
        mimetype="application/dxf",
        as_attachment=True,
        download_name="hatched_output.dxf",
        max_age=0,
    )


if __name__ == "__main__":
    host = os.environ.get("FIBER_LASER_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("FIBER_LASER_WEB_PORT", "5000"))
    debug = os.environ.get("FIBER_LASER_WEB_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)
