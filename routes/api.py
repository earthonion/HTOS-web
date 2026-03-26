import hmac
import json
import os
import shutil
import uuid
import zipfile
from functools import wraps

from quart import Blueprint, Response, abort, request, send_file

from config import CHUNK_DIR, WORKER_KEY
from models import get_db
from services.jobs import get_or_create_job_logger, push_log
from services.titles import lookup_title

api_bp = Blueprint("api", __name__, url_prefix="/api/worker")


def _extract_title_from_zip(zip_path):
    """Read TITLE_ID from param.sfo inside a result zip."""
    import struct

    result = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith("param.sfo"):
                    data = zf.read(name)
                    if len(data) > 20 and data[:4] == b"\x00PSF":
                        key_off = struct.unpack_from("<I", data, 8)[0]
                        data_off = struct.unpack_from("<I", data, 12)[0]
                        count = struct.unpack_from("<I", data, 16)[0]
                        for i in range(count):
                            base = 20 + i * 16
                            k_off = struct.unpack_from("<H", data, base)[0]
                            fmt = struct.unpack_from("<H", data, base + 2)[0]
                            d_len = struct.unpack_from("<I", data, base + 4)[0]
                            d_off = struct.unpack_from("<I", data, base + 12)[0]
                            end = data.index(b"\x00", key_off + k_off)
                            key = data[key_off + k_off : end].decode()
                            if key == "TITLE_ID" and fmt == 0x0204:
                                result[key] = (
                                    data[data_off + d_off : data_off + d_off + d_len]
                                    .rstrip(b"\x00")
                                    .decode()
                                )
                    break
    except Exception:
        pass
    return result


def _validate_result_zip(path):
    """Validate a result zip has actual data, not just headers.
    Returns error string or None if valid."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            entries = zf.infolist()
            if not entries:
                return "Result zip is empty"
            for entry in entries:
                if entry.file_size > 0 and entry.compress_size == 0:
                    return f"Result zip corrupt: {entry.filename} claims {entry.file_size} bytes but has no data"
            # Try reading first entry to verify data is accessible
            with zf.open(entries[0]) as f:
                f.read(1)
    except zipfile.BadZipFile as e:
        return f"Result zip invalid: {e}"
    except OSError as e:
        return f"Result zip unreadable: {e}"
    return None


async def validate_worker_key(key):
    """Check key against DB. Returns user_id or None."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id FROM worker_keys WHERE key = ? AND is_active = 1",
            (key,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE worker_keys SET last_used = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            await db.commit()
            return row["user_id"]
    finally:
        await db.close()
    return None


async def _ensure_global_key_in_db():
    """Insert or update the global worker key in the DB so worker count tracking works."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM worker_keys WHERE key = ?", (WORKER_KEY,)
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE worker_keys SET last_used = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
        else:
            await db.execute(
                "INSERT INTO worker_keys (user_id, key, name, is_active) VALUES (NULL, ?, 'global', 1)",
                (WORKER_KEY,),
            )
            await db.execute(
                "UPDATE worker_keys SET last_used = CURRENT_TIMESTAMP WHERE key = ?",
                (WORKER_KEY,),
            )
        await db.commit()
    finally:
        await db.close()


def require_worker_key(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        key = request.headers.get("X-Worker-Key", "")
        # Global key (backward compat)
        if WORKER_KEY and hmac.compare_digest(key, WORKER_KEY):
            await _ensure_global_key_in_db()
            return await f(*args, **kwargs)
        # User-generated key from DB
        if key and await validate_worker_key(key):
            return await f(*args, **kwargs)
        abort(401)

    return decorated


@api_bp.route("/next", methods=["GET"])
@require_worker_key
async def next_job():
    """Return the next queued job, or 204 if none.
    Workers can pass ?platform=ps5 to only receive jobs for that platform."""
    worker_platform = request.args.get("platform", "ps4")

    # Track worker platform on heartbeat
    worker_key = request.headers.get("X-Worker-Key", "")
    if worker_key:
        db = await get_db()
        try:
            # Check if worker is suspended
            cursor = await db.execute(
                "SELECT suspended_until FROM worker_keys WHERE key = ?", (worker_key,)
            )
            wk = await cursor.fetchone()
            if wk and wk["suspended_until"]:
                cursor2 = await db.execute(
                    "SELECT ? > datetime('now') as is_suspended",
                    (wk["suspended_until"],),
                )
                check = await cursor2.fetchone()
                if check and check["is_suspended"]:
                    return Response(
                        json.dumps({"suspended_until": wk["suspended_until"]}),
                        status=429,
                        content_type="application/json",
                    )
                else:
                    # Suspension expired, clear it
                    await db.execute(
                        "UPDATE worker_keys SET suspended_until = NULL WHERE key = ?",
                        (worker_key,),
                    )

            # Set online_since if worker was offline (last_used > 300s ago or NULL)
            await db.execute(
                "UPDATE worker_keys SET online_since = datetime('now') "
                "WHERE key = ? AND (last_used IS NULL OR last_used < datetime('now', '-300 seconds'))",
                (worker_key,),
            )
            await db.execute(
                "UPDATE worker_keys SET last_platform = ? WHERE key = ?",
                (worker_platform, worker_key),
            )
            if worker_platform == "ps5":
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES ('last_ps5_worker', datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value = datetime('now')"
                )
            await db.commit()
        finally:
            await db.close()

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, operation, params, created_at FROM jobs "
            "WHERE status = 'queued' ORDER BY created_at ASC LIMIT 20"
        )
        rows = await cursor.fetchall()
        if not rows:
            return Response(status=204)

        for row in rows:
            job = dict(row)
            if job["params"]:
                job["params"] = json.loads(job["params"])
            else:
                job["params"] = {}

            # Filter by platform (defaults to ps4 for legacy workers)
            # "unknown" platform treated as ps4 so jobs aren't stuck forever
            job_platform = job["params"].get("platform", "ps4")
            if job_platform == "unknown":
                job_platform = "ps4"
            if job_platform != worker_platform:
                continue

            return job

        # No matching jobs
        return Response(status=204)
    finally:
        await db.close()


@api_bp.route("/jobs/<job_id>/files", methods=["GET"])
@require_worker_key
async def job_files(job_id):
    """Download uploaded files for a job as a zip."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT params FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    params = json.loads(row["params"]) if row["params"] else {}

    # Find the upload directory from params
    upload_dir = params.get("upload_dir") or params.get("saves_dir")
    if params.get("saves_dir"):
        # For reregion, zip the parent dir (contains saves/ and sample/)
        upload_dir = os.path.dirname(params["saves_dir"])

    if not upload_dir or not os.path.isdir(upload_dir):
        abort(404)

    # Create zip on disk to avoid memory issues with large saves
    # Stored in upload_dir's parent so the hourly cleanup cron handles it
    tmp_path = os.path.join(os.path.dirname(upload_dir), f"{job_id}_worker.zip")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        for root, dirs, files in os.walk(upload_dir):
            for f in files:
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, upload_dir)
                zf.write(filepath, arcname)

    return await send_file(
        tmp_path,
        mimetype="application/zip",
        as_attachment=True,
        attachment_filename=f"{job_id}.zip",
    )


@api_bp.route("/jobs/<job_id>/status", methods=["POST"])
@require_worker_key
async def update_status(job_id):
    """Update job status (running/done/failed)."""
    data = await request.get_json()
    if not data or "status" not in data:
        abort(400)

    status = data["status"]
    if status not in ("running", "done", "failed"):
        abort(400)

    worker_key = request.headers.get("X-Worker-Key", "")

    db = await get_db()
    try:
        fields = ["status = ?"]
        values = [status]
        if "error" in data:
            fields.append("error = ?")
            values.append(data["error"])
        if "result_path" in data:
            # Validate result_path stays within workspace
            rp = data["result_path"]
            allowed_prefix = os.path.realpath("workspace")
            if os.path.realpath(rp).startswith(allowed_prefix + os.sep):
                fields.append("result_path = ?")
                values.append(rp)
        # Track which worker handled this job
        if status == "running" and worker_key:
            cursor = await db.execute(
                "SELECT id FROM worker_keys WHERE key = ?", (worker_key,)
            )
            wk_row = await cursor.fetchone()
            if wk_row:
                fields.append("worker_key_id = ?")
                values.append(wk_row["id"])
        values.append(job_id)
        await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
        # Increment jobs_completed counter for this worker key
        if status == "done" and worker_key:
            await db.execute(
                "UPDATE worker_keys SET jobs_completed = jobs_completed + 1 WHERE key = ?",
                (worker_key,),
            )
        # Auto-suspend after 10 consecutive failures
        if status == "failed" and worker_key:
            cursor = await db.execute(
                "SELECT id FROM worker_keys WHERE key = ?", (worker_key,)
            )
            wk = await cursor.fetchone()
            if wk:
                cursor2 = await db.execute(
                    "SELECT status FROM jobs WHERE worker_key_id = ? "
                    "ORDER BY created_at DESC LIMIT 10",
                    (wk["id"],),
                )
                recent = await cursor2.fetchall()
                if len(recent) >= 10 and all(r["status"] == "failed" for r in recent):
                    await db.execute(
                        "UPDATE worker_keys SET suspended_until = datetime('now', '+24 hours') "
                        "WHERE id = ?",
                        (wk["id"],),
                    )
        await db.commit()
    finally:
        await db.close()

    # Extract title from result zip on completion (for decrypt jobs etc.)
    if status == "done":
        try:
            db2 = await get_db()
            try:
                cursor = await db2.execute(
                    "SELECT operation, result_path, params FROM jobs WHERE id = ?",
                    (job_id,),
                )
                jrow = await cursor.fetchone()
                if jrow and jrow["result_path"] and jrow["params"]:
                    jp = json.loads(jrow["params"])
                    rp = jrow["result_path"]
                    if not jp.get("title_id") and os.path.exists(rp):
                        sfo = _extract_title_from_zip(rp)
                        if sfo.get("TITLE_ID"):
                            jp["title_id"] = sfo["TITLE_ID"]
                            title = await lookup_title(sfo["TITLE_ID"]) or ""
                            if title:
                                jp["game_title"] = title
                            await db2.execute(
                                "UPDATE jobs SET params = ? WHERE id = ?",
                                (json.dumps(jp), job_id),
                            )
                            await db2.commit()

                    # Auto-capture sample save from decrypt results
                    tid = jp.get("title_id", "")
                    if tid and jrow["operation"] == "decrypt" and os.path.exists(rp):
                        from services.samples import maybe_store_sample_from_zip

                        platform = jp.get("platform", "ps4")
                        await maybe_store_sample_from_zip(tid, rp, platform)
            finally:
                await db2.close()
        except Exception:
            pass

    # Also broadcast status change via SSE
    job = get_or_create_job_logger(job_id)
    if job:
        job.status = status
        if "result_path" in data:
            job.result_path = data["result_path"]
        if "error" in data:
            job.error = data["error"]
        entry = {"level": "STATUS", "msg": status}
        job.logger._broadcast(entry)

    return {"ok": True}


@api_bp.route("/jobs/<job_id>/log", methods=["POST"])
@require_worker_key
async def post_log(job_id):
    """Push a log line to the job's SSE stream and persist to DB."""
    data = await request.get_json()
    if not data or "msg" not in data:
        abort(400)

    level = data.get("level", "INFO")
    msg = data["msg"]
    push_log(job_id, level, msg)

    # Persist log entry to DB
    entry = json.dumps({"level": level, "msg": msg})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET logs = CASE WHEN logs IS NULL THEN ? ELSE logs || '\n' || ? END WHERE id = ?",
            (entry, entry, job_id),
        )
        await db.commit()
    finally:
        await db.close()

    return {"ok": True}


@api_bp.route("/jobs/<job_id>/result", methods=["POST"])
@require_worker_key
async def upload_result(job_id):
    """Upload result file (binary body)."""
    result_dir = os.path.join("workspace", "results")
    os.makedirs(result_dir, exist_ok=True)
    result_path = os.path.join(result_dir, f"{job_id}.zip")

    body = await request.get_data()
    with open(result_path, "wb") as f:
        f.write(body)

    # Validate the zip before accepting
    err = _validate_result_zip(result_path)
    if err:
        os.remove(result_path)
        # Mark job as failed
        db = await get_db()
        try:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                (err, job_id),
            )
            await db.commit()
        finally:
            await db.close()
        push_log(job_id, "ERROR", err)
        return {"ok": False, "error": err}, 400

    # Update job with result path
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET result_path = ? WHERE id = ?", (result_path, job_id)
        )
        await db.commit()
    finally:
        await db.close()

    # Update in-memory job too
    job = get_or_create_job_logger(job_id)
    if job:
        job.result_path = result_path

    return {"ok": True, "result_path": result_path}


@api_bp.route("/jobs/<job_id>/result/init", methods=["POST"])
@require_worker_key
async def init_result_upload(job_id):
    """Start a chunked result upload."""
    data = await request.get_json()
    if not data or "total_size" not in data:
        abort(400)

    upload_id = str(uuid.uuid4())
    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    os.makedirs(chunk_dir, exist_ok=True)

    meta = {
        "job_id": job_id,
        "total_size": data["total_size"],
    }
    import time

    meta["created_at"] = time.time()
    with open(os.path.join(chunk_dir, "meta.json"), "w") as f:
        json.dump(meta, f)

    return {"upload_id": upload_id}


@api_bp.route("/jobs/<job_id>/result/chunk/<int:index>", methods=["POST"])
@require_worker_key
async def upload_result_chunk(job_id, index):
    """Upload one chunk of a result file."""
    # Find the upload_id from query param
    upload_id = request.args.get("upload_id", "")
    if not upload_id:
        abort(400)

    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    if not os.path.isdir(chunk_dir):
        abort(404)

    body = await request.get_data()
    if not body:
        abort(400)

    chunk_path = os.path.join(chunk_dir, f"{index}.part")
    with open(chunk_path, "wb") as f:
        f.write(body)

    return {"ok": True, "index": index}


@api_bp.route("/jobs/<job_id>/result/complete", methods=["POST"])
@require_worker_key
async def complete_result_upload(job_id):
    """Assemble chunked result and set result_path."""
    data = await request.get_json()
    if not data or "upload_id" not in data:
        abort(400)

    upload_id = data["upload_id"]
    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    if not os.path.isdir(chunk_dir):
        abort(404)

    # Sort and assemble chunks
    parts = sorted(
        [f for f in os.listdir(chunk_dir) if f.endswith(".part")],
        key=lambda x: int(x.replace(".part", "")),
    )
    if not parts:
        abort(400)

    result_dir = os.path.join("workspace", "results")
    os.makedirs(result_dir, exist_ok=True)
    result_path = os.path.join(result_dir, f"{job_id}.zip")

    with open(result_path, "wb") as out:
        for part in parts:
            part_path = os.path.join(chunk_dir, part)
            with open(part_path, "rb") as inp:
                shutil.copyfileobj(inp, out)

    # Clean up chunk dir
    shutil.rmtree(chunk_dir, ignore_errors=True)

    # Validate the assembled zip
    err = _validate_result_zip(result_path)
    if err:
        os.remove(result_path)
        db = await get_db()
        try:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                (err, job_id),
            )
            await db.commit()
        finally:
            await db.close()
        push_log(job_id, "ERROR", err)
        return {"ok": False, "error": err}, 400

    # Update job with result path
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET result_path = ? WHERE id = ?", (result_path, job_id)
        )
        await db.commit()
    finally:
        await db.close()

    job = get_or_create_job_logger(job_id)
    if job:
        job.result_path = result_path

    return {"ok": True, "result_path": result_path}
