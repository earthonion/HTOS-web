"""Public REST API v1 for programmatic save management."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from functools import wraps

from quart import Blueprint, jsonify, request, send_file

from models import get_db
from routes.api import get_worker_api_access, validate_worker_key
from services.files import (
    DangerousFileError,
    FileTooLargeError,
    InvalidSaveFilesError,
    detect_platform_for_operation,
    extract_account_id,
    resolve_chunked_uploads,
    save_uploaded_files,
    validate_save_pairs,
)
from services.jobs import create_job
from services.rate_limit import enforce_job_rate_limit

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
API_OPERATION_LOG_MESSAGES = {
    "decrypt": "Decrypting with GarlicSaves",
    "encrypt": "Encrypting with GarlicSaves",
    "resign": "Resigning with GarlicSaves",
    "reregion": "Re-regioning with GarlicSaves",
    "createsave": "Creating save with GarlicSaves",
    "ppr_derive": "Deriving PPR with GarlicSaves",
}


async def log_api_operation(job, operation):
    message = API_OPERATION_LOG_MESSAGES[operation]
    job.logger.info(message)
    entry = json.dumps({"level": "INFO", "msg": message})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET logs = CASE WHEN logs IS NULL THEN ? "
            "ELSE logs || '\n' || ? END WHERE id = ?",
            (entry, entry, job.job_id),
        )
        await db.commit()
    finally:
        await db.close()


def _shared_key_legacy_active():
    value = os.getenv("SHARED_KEY_LEGACY_UNTIL", "").strip()
    if not value:
        return False
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < cutoff


def _shared_key_retired():
    return bool(os.getenv("SHARED_KEY_LEGACY_UNTIL", "").strip()) and not _shared_key_legacy_active()


def require_api_key(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        key = request.headers.get("X-Worker-Key", "")
        if not key:
            return jsonify({"error": "Missing X-Worker-Key header"}), 401
        user_id = await validate_worker_key(key)
        if user_id is None:
            return jsonify({"error": "Invalid API key"}), 401
        access = await get_worker_api_access(key)
        if not access:
            return jsonify({"error": "Invalid API key"}), 401
        if access["is_shared"] and _shared_key_retired():
            return jsonify({"error": "The shared API key has been retired; authorize this installation"}), 401
        kwargs["user_id"] = user_id
        kwargs["access"] = access
        return await f(*args, **kwargs)

    return decorated


async def record_submitter_ip(user_id, key_id, client_ip):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET last_ip = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?",
            (client_ip, user_id),
        )
        await db.execute(
            "UPDATE worker_keys SET last_ip = ? WHERE id = ?",
            (client_ip, key_id),
        )
        await db.commit()
    finally:
        await db.close()


@api_v1_bp.route("/jobs", methods=["POST"])
@require_api_key
async def create_job_endpoint(user_id, access):
    from auth import _real_client_ip

    form = await request.form
    operation = form.get("operation", "")
    if operation == "ppr_derive":
        if not access["ppr_derive_enabled"]:
            return jsonify({"error": "ppr_derive is disabled for this worker key"}), 403
    elif not access["api_enabled"]:
        return jsonify(
            {"error": "API job creation is disabled for this worker key"}
        ), 403

    client_ip = _real_client_ip()
    _, retry_after = await enforce_job_rate_limit(
        request.headers.get("X-Worker-Key", ""),
        incoming_bytes=request.content_length or 0,
        client_ip=client_ip,
    )
    if retry_after:
        return (
            jsonify({"error": "rate limit exceeded; try again later"}),
            429,
            {"Retry-After": str(retry_after)},
        )
    key_id = access["id"]
    installation_id = access["installation_id"]
    await record_submitter_ip(user_id, key_id, client_ip)
    if operation == "ppr_derive":
        client_value = form.get("client_value", "").lower()
        server_value = form.get("server_value", "").lower()
        for name, value in (
            ("client_value", client_value),
            ("server_value", server_value),
        ):
            if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
                return jsonify(
                    {"error": f"{name} must be exactly 16 bytes of hex"}
                ), 400
        job = await create_job(
            user_id,
            operation,
            {
                "client_value": client_value,
                "server_value": server_value,
                "platform": "ps5",
            },
            ready=True,
            submitter_key_id=key_id,
            submitter_installation_id=installation_id,
            submitter_ip=client_ip,
        )
        await log_api_operation(job, operation)
        return jsonify({
            "job_id": job.job_id, "status": "queued", "job_token": job.read_token,
        }), 201

    if operation not in ("decrypt", "encrypt", "resign", "reregion", "createsave"):
        return jsonify({"error": f"Invalid operation: {operation}"}), 400

    files = (await request.files).getlist("files")
    upload_ids_json = form.get("upload_ids", "")
    if (not files or not files[0].filename) and not upload_ids_json:
        return jsonify({"error": "No files uploaded"}), 400

    # Create job for file storage
    params = {}
    job = await create_job(
        user_id,
        operation,
        params,
        ready=False,
        submitter_key_id=key_id,
        submitter_installation_id=installation_id,
        submitter_ip=client_ip,
    )

    try:
        if upload_ids_json and not (files and files[0].filename):
            upload_ids = json.loads(upload_ids_json)
            if (
                not isinstance(upload_ids, list)
                or not upload_ids
                or not all(
                    isinstance(uid, str)
                    and len(uid) == 36
                    and set(uid) <= set("0123456789abcdef-")
                    for uid in upload_ids
                )
            ):
                return jsonify(
                    {"error": "upload_ids must be a list of upload IDs"}
                ), 400
            upload_dir = await resolve_chunked_uploads(upload_ids, user_id, job.job_id)
        else:
            upload_dir = await save_uploaded_files(files, user_id, job.job_id)
    except FileTooLargeError as e:
        return jsonify({"error": f"File too large: {e}"}), 400
    except DangerousFileError as e:
        return jsonify({"error": str(e)}), 400

    uploaded_bytes = sum(
        os.path.getsize(os.path.join(root, filename))
        for root, _, filenames in os.walk(upload_dir)
        for filename in filenames
    )
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET submitter_bytes = ? WHERE id = ?",
            (uploaded_bytes, job.job_id),
        )
        await db.commit()
    finally:
        await db.close()

    try:
        platform = detect_platform_for_operation(upload_dir, operation)
    except InvalidSaveFilesError as e:
        return jsonify({"error": str(e)}), 400

    params["upload_dir"] = upload_dir
    params["platform"] = platform

    if operation == "decrypt":
        if platform != "ps5":
            try:
                validate_save_pairs(upload_dir)
            except InvalidSaveFilesError as e:
                return jsonify({"error": str(e)}), 400
        include_sce_sys = form.get("include_sce_sys", "").lower() in ("true", "1", "on")
        params["include_sce_sys"] = include_sce_sys
        savename = ""
        for f in os.listdir(upload_dir):
            if f.endswith(".bin") and not f.startswith("."):
                savename = os.path.splitext(f)[0]
                break
        if savename:
            params["savename"] = savename
        acct = extract_account_id(upload_dir, platform)
        if acct:
            params["sfo_account_id"] = acct

    elif operation in ("encrypt", "createsave"):
        account_id = form.get("account_id", "")
        if not account_id:
            return jsonify({"error": "Missing required field: account_id"}), 400
        params["account_id"] = account_id
        if form.get("saveblocks"):
            params["saveblocks"] = int(form["saveblocks"])
        if operation == "createsave":
            savename = form.get("savename", "")
            saveblocks = form.get("saveblocks", "")
            if not savename or not saveblocks:
                return jsonify(
                    {"error": "Missing required field: savename or saveblocks"}
                ), 400
            params["savename"] = savename
            params["saveblocks"] = int(saveblocks)

    elif operation == "resign":
        account_id = form.get("account_id", "")
        if not account_id:
            return jsonify({"error": "Missing required field: account_id"}), 400
        params["account_id"] = account_id
        if platform != "ps5":
            try:
                validate_save_pairs(upload_dir)
            except InvalidSaveFilesError as e:
                return jsonify({"error": str(e)}), 400
        savename = ""
        for f in os.listdir(upload_dir):
            if f.endswith(".bin") and not f.startswith("."):
                savename = os.path.splitext(f)[0]
                break
        if savename:
            params["savename"] = savename

    elif operation == "reregion":
        account_id = form.get("account_id", "")
        if not account_id:
            return jsonify({"error": "Missing required field: account_id"}), 400
        params["account_id"] = account_id
        sample_files = (await request.files).getlist("sample")
        if not sample_files or not sample_files[0].filename:
            return jsonify({"error": "Missing sample files"}), 400
        import uuid

        sample_id = str(uuid.uuid4())
        try:
            sample_dir = await save_uploaded_files(sample_files, user_id, sample_id)
        except (FileTooLargeError, DangerousFileError) as e:
            return jsonify({"error": str(e)}), 400
        params["saves_dir"] = upload_dir
        params["sample_dir"] = sample_dir

    await job.update_params(params)
    await job.set_status("queued")
    await log_api_operation(job, operation)

    return jsonify({
        "job_id": job.job_id, "status": "queued", "job_token": job.read_token,
    }), 201


@api_v1_bp.route("/jobs", methods=["GET"])
@require_api_key
async def list_jobs(user_id, access):
    if access["is_shared"]:
        return jsonify({"error": "Job listing is disabled for shared credentials"}), 403
    installation_id = access["installation_id"]
    owner_sql = " AND submitter_installation_id = ?" if installation_id else ""
    owner_params = (user_id, installation_id) if installation_id else (user_id,)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, operation, status, created_at FROM jobs "
            f"WHERE user_id = ?{owner_sql} ORDER BY created_at DESC LIMIT 50",
            owner_params,
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return jsonify(
        [
            {
                "id": r["id"],
                "operation": r["operation"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    )


def _job_read_allowed(access, row):
    if not access["is_shared"] or _shared_key_legacy_active():
        return True
    supplied = request.headers.get("X-Job-Token", "")
    stored = row["read_token_hash"] or ""
    if not supplied or not stored:
        return False
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    return hmac.compare_digest(digest, stored)


@api_v1_bp.route("/jobs/<job_id>", methods=["GET"])
@require_api_key
async def get_job(user_id, access, job_id):
    installation_id = access["installation_id"]
    owner_sql = " AND submitter_installation_id = ?" if installation_id else ""
    owner_params = (job_id, user_id, installation_id) if installation_id else (job_id, user_id)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, operation, status, created_at, error, logs, read_token_hash "
            f"FROM jobs WHERE id = ? AND user_id = ?{owner_sql}",
            owner_params,
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row or not _job_read_allowed(access, row):
        return jsonify({"error": "Job not found"}), 404

    logs = []
    if row["logs"]:
        for line in row["logs"].split("\n"):
            try:
                logs.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass

    return jsonify(
        {
            "id": row["id"],
            "operation": row["operation"],
            "status": row["status"],
            "created_at": row["created_at"],
            "error": row["error"],
            "logs": logs,
        }
    )


@api_v1_bp.route("/jobs/<job_id>/result", methods=["GET"])
@require_api_key
async def get_result(user_id, access, job_id):
    installation_id = access["installation_id"]
    owner_sql = " AND submitter_installation_id = ?" if installation_id else ""
    owner_params = (job_id, user_id, installation_id) if installation_id else (job_id, user_id)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT operation, status, result_path, read_token_hash FROM jobs "
            f"WHERE id = ? AND user_id = ?{owner_sql}",
            owner_params,
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row or not _job_read_allowed(access, row):
        return jsonify({"error": "Job not found"}), 404
    if row["status"] != "done":
        return jsonify({"error": "Job not done yet"}), 400
    if not row["result_path"] or not os.path.exists(row["result_path"]):
        return jsonify({"error": "Result file not found"}), 404

    if os.getenv("DIRECT_FILE_DOWNLOAD") == "1":
        return await send_file(
            row["result_path"],
            as_attachment=True,
            attachment_filename=os.path.basename(row["result_path"]),
        )

    # Delegate byte-streaming to nginx via X-Accel-Redirect. See
    # /_result_files/ internal alias in the nginx site config.
    from quart import Response

    filename = os.path.basename(row["result_path"])
    resp = Response(b"", status=200)
    resp.headers["X-Accel-Redirect"] = f"/_result_files/{filename}"
    if row["operation"] == "ppr_derive":
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Content-Disposition"] = f'attachment; filename="{job_id}.json"'
    else:
        resp.headers["Content-Type"] = "application/zip"
        resp.headers["Content-Disposition"] = f'attachment; filename="{job_id}.zip"'
    return resp
