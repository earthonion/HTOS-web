"""UE4 Save Editor — parse GVAS .sav files, edit properties, download modified saves."""

import io
import json
import os
import uuid
import zipfile

from quart import Blueprint, jsonify, render_template, request, send_file

from auth import login_required

saveeditor_bp = Blueprint("saveeditor", __name__)


def _patch_savconverter():
    """Fix SavConverter's write_date_time to handle datetimes without microseconds."""
    try:
        from datetime import datetime
        from struct import pack

        from SavConverter import SavWriter

        _orig = SavWriter.write_date_time

        def _patched(date_time_string):
            if isinstance(date_time_string, int):
                return _orig(date_time_string)
            try:
                return _orig(date_time_string)
            except ValueError:
                dt = datetime.strptime(date_time_string, "%Y-%m-%d %H:%M:%S")
                ts_ms = int((dt - datetime(1970, 1, 1)).total_seconds() * 1000)
                ticks = (ts_ms + 62135596800000) * 10000
                return pack("<Q", ticks)

        SavWriter.write_date_time = _patched
        # Also patch in SavProperties since it does `from .SavWriter import *`
        from SavConverter import SavProperties

        SavProperties.write_date_time = _patched
    except ImportError:
        pass


_patch_savconverter()

WORKSPACE = "workspace/saveeditor"


def _ensure_workspace():
    os.makedirs(WORKSPACE, exist_ok=True)


def _session_dir(session_id):
    return os.path.join(WORKSPACE, session_id)


def _is_gvas(data: bytes) -> bool:
    """Check if data starts with GVAS magic."""
    return data[:4] == b"GVAS"


@saveeditor_bp.route("/saveeditor", methods=["GET"])
@login_required
async def saveeditor():
    return await render_template("saveeditor.html")


@saveeditor_bp.route("/saveeditor/upload", methods=["POST"])
@login_required
async def saveeditor_upload():
    """Upload a .sav file, parse it, return JSON tree."""
    files = (await request.files).getlist("file")
    if not files or not files[0].filename:
        return jsonify({"error": "No file uploaded"}), 400

    f = files[0]
    raw = f.read()
    filename = f.filename

    # Handle zip — extract first .sav
    if filename.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for name in zf.namelist():
                if name.lower().endswith(".sav") and not name.startswith("__MACOSX"):
                    raw = zf.read(name)
                    filename = os.path.basename(name)
                    break
            else:
                return jsonify({"error": "No .sav file found in zip"}), 400
        except zipfile.BadZipFile:
            return jsonify({"error": "Invalid zip file"}), 400

    if not _is_gvas(raw):
        return jsonify(
            {"error": "Not a valid UE4 save file (missing GVAS header)"}
        ), 400

    try:
        from SavConverter import read_sav, sav_to_json
    except ImportError:
        return jsonify({"error": "SavConverter not installed on server"}), 500

    # Store original file (read_sav needs a file path)
    sid = uuid.uuid4().hex[:12]
    _ensure_workspace()
    sdir = _session_dir(sid)
    os.makedirs(sdir, exist_ok=True)
    sav_path = os.path.join(sdir, "original.sav")
    with open(sav_path, "wb") as out:
        out.write(raw)
    with open(os.path.join(sdir, "meta.json"), "w") as out:
        json.dump({"filename": filename}, out)

    try:
        raw_props = read_sav(sav_path)
        parsed = sav_to_json(raw_props)
    except Exception as e:
        return jsonify({"error": f"Failed to parse save: {e}"}), 400

    return jsonify(
        {
            "session_id": sid,
            "filename": filename,
            "properties": parsed,
        }
    )


@saveeditor_bp.route("/saveeditor/save", methods=["POST"])
@login_required
async def saveeditor_save():
    """Receive modified JSON, rebuild .sav, return download."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    sid = data.get("session_id", "")
    properties = data.get("properties")
    if not sid or not properties:
        return jsonify({"error": "Missing session_id or properties"}), 400
    if not sid.isalnum():
        return jsonify({"error": "Invalid session_id"}), 400

    sdir = _session_dir(sid)
    if not os.path.isdir(sdir):
        return jsonify({"error": "Session not found — re-upload the file"}), 404

    try:
        from SavConverter import json_to_sav
    except ImportError:
        return jsonify({"error": "SavConverter not installed on server"}), 500

    try:
        rebuilt = json_to_sav(properties)
    except Exception as e:
        return jsonify({"error": f"Failed to rebuild save: {e}"}), 400

    # Load original filename
    meta_path = os.path.join(sdir, "meta.json")
    filename = "modified.sav"
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        filename = meta.get("filename", filename)

    out_path = os.path.join(sdir, "modified.sav")
    with open(out_path, "wb") as f:
        f.write(rebuilt)

    return await send_file(out_path, as_attachment=True, attachment_filename=filename)
