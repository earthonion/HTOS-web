"""Save Editor — parse UE4/UE5 GVAS .sav files, edit properties, download modified saves."""

import io
import json
import os
import struct
import uuid
import zipfile

from quart import Blueprint, jsonify, render_template, request, send_file

from auth import login_required

saveeditor_bp = Blueprint("saveeditor", __name__)


def _patch_savconverter():
    """Fix SavConverter bugs: datetime format and GVAS v1 header support."""
    try:
        from datetime import datetime
        from struct import pack

        from SavConverter import SavProperties, SavWriter

        # Fix 1: write_date_time doesn't handle datetimes without microseconds
        _orig = SavWriter.write_date_time

        def _patched_dt(date_time_string):
            if isinstance(date_time_string, int):
                return _orig(date_time_string)
            try:
                return _orig(date_time_string)
            except ValueError:
                dt = datetime.strptime(date_time_string, "%Y-%m-%d %H:%M:%S")
                ts_ms = int((dt - datetime(1970, 1, 1)).total_seconds() * 1000)
                ticks = (ts_ms + 62135596800000) * 10000
                return pack("<Q", ticks)

        SavWriter.write_date_time = _patched_dt
        SavProperties.write_date_time = _patched_dt

    except ImportError:
        pass


_patch_savconverter()

WORKSPACE = "workspace/saveeditor"


def _ensure_workspace():
    os.makedirs(WORKSPACE, exist_ok=True)


def _session_dir(session_id):
    return os.path.join(WORKSPACE, session_id)


def _is_gvas(data: bytes) -> bool:
    return data[:4] == b"GVAS"


def _gvas_version(data: bytes) -> int:
    """Read save_game_version (int32 at offset 4)."""
    if len(data) < 8:
        return 0
    return struct.unpack_from("<i", data, 4)[0]


# ── UE5 (palworld-save-tools) → SavConverter-style normalization ──


def _ue5_props_to_flat(properties: dict) -> list:
    """Convert palworld-save-tools property dict to SavConverter-style flat list."""
    result = []
    for name, prop in properties.items():
        result.append(_ue5_prop_to_flat(name, prop))
    return result


def _ue5_prop_to_flat(name: str, prop: dict) -> dict:
    """Convert a single palworld-save-tools property to SavConverter format."""
    ptype = prop.get("type", "")
    out = {"type": ptype, "name": name}

    if ptype == "StructProperty":
        out["subtype"] = prop.get("struct_type", "")
        struct_val = prop.get("value", {})
        if isinstance(struct_val, dict):
            out["value"] = _ue5_props_to_flat(struct_val)
        else:
            out["value"] = struct_val
    elif ptype == "ArrayProperty":
        arr_type = prop.get("array_type", "")
        out["subtype"] = arr_type
        raw_val = prop.get("value", {})
        values = raw_val.get("values", []) if isinstance(raw_val, dict) else raw_val
        if arr_type == "StructProperty":
            converted = []
            for item in values:
                if isinstance(item, dict):
                    converted.append(_ue5_props_to_flat(item))
                else:
                    converted.append(item)
            out["value"] = converted
        else:
            out["value"] = values
    elif ptype == "MapProperty":
        out["subtype"] = prop.get("key_type", "")
        raw_val = prop.get("value", [])
        out["value"] = raw_val
    elif ptype == "BoolProperty":
        out["value"] = prop.get("value", False)
    elif ptype == "EnumProperty":
        out["subtype"] = (
            prop.get("value", {}).get("enum_type", "")
            if isinstance(prop.get("value"), dict)
            else ""
        )
        out["value"] = (
            prop.get("value", {}).get("value", "")
            if isinstance(prop.get("value"), dict)
            else prop.get("value", "")
        )
    else:
        val = prop.get("value", "")
        out["value"] = val

    return out


def _flat_to_ue5_props(flat_list: list, original_props: dict) -> dict:
    """Convert SavConverter-style flat list back to palworld-save-tools dict.
    Uses original_props as a template for metadata we don't expose in the editor."""
    result = {}
    for item in flat_list:
        name = item.get("name", "")
        if not name:
            continue
        orig = original_props.get(name, {})
        result[name] = _flat_to_ue5_prop(item, orig)
    return result


def _flat_to_ue5_prop(flat: dict, orig: dict) -> dict:
    """Convert a single flat property back to palworld-save-tools format."""
    ptype = flat.get("type", "")
    out = dict(orig)  # start with original to preserve metadata
    out["type"] = ptype

    if ptype == "StructProperty":
        out["struct_type"] = flat.get("subtype", orig.get("struct_type", ""))
        flat_val = flat.get("value", [])
        if isinstance(flat_val, list):
            orig_val = orig.get("value", {})
            if isinstance(orig_val, dict):
                out["value"] = _flat_to_ue5_props(flat_val, orig_val)
            else:
                out["value"] = flat_val
        else:
            out["value"] = flat_val
    elif ptype == "ArrayProperty":
        arr_type = flat.get("subtype", orig.get("array_type", ""))
        out["array_type"] = arr_type
        flat_val = flat.get("value", [])
        orig_raw = orig.get("value", {})
        if arr_type == "StructProperty" and isinstance(flat_val, list):
            converted = []
            orig_values = (
                orig_raw.get("values", []) if isinstance(orig_raw, dict) else []
            )
            for i, item in enumerate(flat_val):
                if isinstance(item, list):
                    orig_item = orig_values[i] if i < len(orig_values) else {}
                    converted.append(
                        _flat_to_ue5_props(
                            item, orig_item if isinstance(orig_item, dict) else {}
                        )
                    )
                else:
                    converted.append(item)
            if isinstance(orig_raw, dict):
                out["value"] = dict(orig_raw)
                out["value"]["values"] = converted
            else:
                out["value"] = converted
        else:
            if isinstance(orig_raw, dict):
                out["value"] = dict(orig_raw)
                out["value"]["values"] = flat_val
            else:
                out["value"] = flat_val
    elif ptype == "MapProperty":
        out["value"] = flat.get("value", orig.get("value", []))
    elif ptype == "BoolProperty":
        out["value"] = flat.get("value", False)
    elif ptype == "EnumProperty":
        orig_val = orig.get("value", {})
        if isinstance(orig_val, dict):
            out["value"] = dict(orig_val)
            out["value"]["value"] = flat.get("value", "")
        else:
            out["value"] = flat.get("value", "")
    else:
        out["value"] = flat.get("value", "")

    return out


# ── Routes ──


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
            {"error": "Not a valid GVAS save file (missing GVAS header)"}
        ), 400

    version = _gvas_version(raw)
    if version < 2:
        return jsonify({"error": "GVAS v1 saves are not yet supported. This is an older UE4 format."}), 400

    # Store original file
    sid = uuid.uuid4().hex[:12]
    _ensure_workspace()
    sdir = _session_dir(sid)
    os.makedirs(sdir, exist_ok=True)
    sav_path = os.path.join(sdir, "original.sav")
    with open(sav_path, "wb") as out:
        out.write(raw)

    engine_version = ""
    save_class = ""

    if version == 3:
        # UE5 — use palworld-save-tools
        try:
            from palworld_save_tools.gvas import GvasFile
        except ImportError:
            return jsonify(
                {"error": "palworld-save-tools not installed on server"}
            ), 500
        try:
            gvas = GvasFile.read(raw, allow_nan=True)
            header = gvas.header.dump()
            engine_version = f"{header.get('engine_version_major', '')}.{header.get('engine_version_minor', '')}.{header.get('engine_version_patch', '')}"
            save_class = header.get("save_game_class_name", "")
            parsed = _ue5_props_to_flat(gvas.properties)
            # Prepend a synthetic header for the frontend
            parsed.insert(
                0,
                {
                    "type": "HeaderProperty",
                    "engine_version": engine_version,
                    "save_game_class_name": save_class,
                    "save_game_version": 3,
                },
            )
            engine_label = f"UE5 ({engine_version})"
        except Exception as e:
            return jsonify({"error": f"Failed to parse UE5 save: {e}"}), 400
    else:
        # UE4 — use SavConverter
        try:
            from SavConverter import read_sav, sav_to_json
        except ImportError:
            return jsonify({"error": "SavConverter not installed on server"}), 500
        try:
            raw_props = read_sav(sav_path)
            parsed = sav_to_json(raw_props)
        except Exception as e:
            return jsonify({"error": f"Failed to parse UE4 save: {e}"}), 400
        hdr = next(
            (
                p
                for p in parsed
                if isinstance(p, dict) and p.get("type") == "HeaderProperty"
            ),
            None,
        )
        engine_version = hdr.get("engine_version", "") if hdr else ""
        engine_label = f"UE4 ({engine_version})" if engine_version else "UE4"

    with open(os.path.join(sdir, "meta.json"), "w") as out:
        json.dump({"filename": filename, "version": version}, out)

    return jsonify(
        {
            "session_id": sid,
            "filename": filename,
            "engine": engine_label,
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

    meta_path = os.path.join(sdir, "meta.json")
    filename = "modified.sav"
    version = 2
    if os.path.exists(meta_path):
        with open(meta_path) as mf:
            meta = json.load(mf)
        filename = meta.get("filename", filename)
        version = meta.get("version", 2)

    sav_path = os.path.join(sdir, "original.sav")

    if version == 3:
        # UE5 — rebuild via palworld-save-tools
        try:
            from palworld_save_tools.gvas import GvasFile
        except ImportError:
            return jsonify(
                {"error": "palworld-save-tools not installed on server"}
            ), 500
        try:
            with open(sav_path, "rb") as sf:
                original_raw = sf.read()
            gvas = GvasFile.read(original_raw, allow_nan=True)
            # Strip synthetic header from flat list
            editable = [p for p in properties if p.get("type") != "HeaderProperty"]
            gvas.properties = _flat_to_ue5_props(editable, gvas.properties)
            rebuilt = gvas.write()
        except Exception as e:
            return jsonify({"error": f"Failed to rebuild UE5 save: {e}"}), 400
    else:
        # UE4 — rebuild via SavConverter
        try:
            from SavConverter import json_to_sav
        except ImportError:
            return jsonify({"error": "SavConverter not installed on server"}), 500
        try:
            rebuilt = json_to_sav(properties)
        except Exception as e:
            return jsonify({"error": f"Failed to rebuild save: {e}"}), 400

    out_path = os.path.join(sdir, "modified.sav")
    with open(out_path, "wb") as f:
        f.write(rebuilt)

    return await send_file(out_path, as_attachment=True, attachment_filename=filename)
