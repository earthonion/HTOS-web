"""Public REST API v1 for programmatic save management."""

import json
import os
from functools import wraps

from quart import Blueprint, jsonify, request, send_file

from models import get_db
from routes.api import validate_worker_key
from services.files import (
    DangerousFileError,
    FileTooLargeError,
    InvalidSaveFilesError,
    detect_platform_in_dir,
    extract_account_id,
    save_uploaded_files,
    validate_save_pairs,
)
from services.jobs import create_job

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def require_api_key(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        key = request.headers.get("X-Worker-Key", "")
        if not key:
            return jsonify({"error": "Missing X-Worker-Key header"}), 401
        user_id = await validate_worker_key(key)
        if user_id is None:
            return jsonify({"error": "Invalid API key"}), 401
        kwargs["user_id"] = user_id
        return await f(*args, **kwargs)

    return decorated


@api_v1_bp.route("/jobs", methods=["POST"])
@require_api_key
async def create_job_endpoint(user_id):
    form = await request.form
    operation = form.get("operation", "")
    if operation not in ("decrypt", "encrypt", "resign", "reregion", "createsave"):
        return jsonify({"error": f"Invalid operation: {operation}"}), 400

    files = (await request.files).getlist("files")
    if not files or not files[0].filename:
        return jsonify({"error": "No files uploaded"}), 400

    # Create job for file storage
    params = {}
    job = await create_job(user_id, operation, params, ready=False)

    try:
        upload_dir = await save_uploaded_files(files, user_id, job.job_id)
    except FileTooLargeError as e:
        return jsonify({"error": f"File too large: {e}"}), 400
    except DangerousFileError as e:
        return jsonify({"error": str(e)}), 400

    platform = detect_platform_in_dir(upload_dir)
    if platform == "unknown":
        platform = "ps4"

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
        if form.get("platform") == "ps5":
            params["platform"] = "ps5"
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

    return jsonify({"job_id": job.job_id, "status": "queued"}), 201


@api_v1_bp.route("/jobs", methods=["GET"])
@require_api_key
async def list_jobs(user_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, operation, status, created_at FROM jobs "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user_id,),
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


@api_v1_bp.route("/jobs/<job_id>", methods=["GET"])
@require_api_key
async def get_job(user_id, job_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, operation, status, created_at, error, logs FROM jobs "
            "WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
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
async def get_result(user_id, job_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT status, result_path FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        return jsonify({"error": "Job not found"}), 404
    if row["status"] != "done":
        return jsonify({"error": "Job not done yet"}), 400
    if not row["result_path"] or not os.path.exists(row["result_path"]):
        return jsonify({"error": "Result file not found"}), 404

    return await send_file(
        row["result_path"], as_attachment=True, attachment_filename=f"{job_id}.zip"
    )
