import asyncio
import json
import os
import re
import zipfile

from quart import Blueprint, Response, abort, render_template, send_file, session

from auth import login_required
from models import get_db
from services.files import extract_account_id_from_zip
from services.jobs import get_or_create_job_logger
from services.titles import lookup_title

jobs_bp = Blueprint("jobs", __name__)


async def _load_job_from_db(job_id, user_id):
    """Load job from DB and ensure it's in the in-memory registry for SSE."""
    # Always check DB for latest state (workers may run in separate processes)
    db = await get_db()
    try:
        if user_id is None:
            cursor = await db.execute(
                "SELECT id, user_id, operation, status, result_path, error, params, logs FROM jobs WHERE id = ?",
                (job_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, operation, status, result_path, error, params, logs FROM jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        return None

    # Get or create in-memory job for SSE, then sync from DB
    job = get_or_create_job_logger(job_id)
    job.user_id = row["user_id"]
    job.operation = row["operation"]
    job.status = row["status"]
    job.result_path = row["result_path"]
    job.error = row["error"]
    if row["params"]:
        job.params = json.loads(row["params"])
    # Load persisted logs into in-memory logger if empty
    if not job.logger.messages and row["logs"]:
        for line in row["logs"].split("\n"):
            try:
                entry = json.loads(line)
                job.logger.messages.append(entry)
            except (json.JSONDecodeError, ValueError):
                pass
    return job


@jobs_bp.route("/jobs/<job_id>")
@login_required
async def job_status(job_id):
    user_id = None if session.get("is_admin") else session["user_id"]
    job = await _load_job_from_db(job_id, user_id)
    if not job:
        abort(404)

    # Extract account ID and title from result zip if not already in params
    if job.status == "done" and job.result_path and os.path.exists(job.result_path):
        updates = {}
        if not job.params.get("sfo_account_id"):
            platform = job.params.get("platform", "ps4")
            acct = extract_account_id_from_zip(job.result_path, platform)
            if acct:
                job.params["sfo_account_id"] = acct
                updates["sfo_account_id"] = acct
        if not job.params.get("title_id"):
            sfo = _extract_sfo_fields_from_zip(job.result_path)
            tid = sfo.get("TITLE_ID", "")
            if tid:
                job.params["title_id"] = tid
                updates["title_id"] = tid
                title = await lookup_title(tid) or ""
                if title:
                    job.params["game_title"] = title
                    updates["game_title"] = title
        if updates:
            await job.update_params(updates)

    queue_position = 0
    queue_total = 0
    if job.status == "queued":
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND created_at <= "
                "(SELECT created_at FROM jobs WHERE id = ?)",
                (job_id,),
            )
            queue_position = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
            )
            queue_total = (await cursor.fetchone())[0]
        finally:
            await db.close()

    return await render_template(
        "job_status.html",
        job=job,
        queue_position=queue_position,
        queue_total=queue_total,
    )


@jobs_bp.route("/jobs/<job_id>/queue")
@login_required
async def job_queue_position(job_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND created_at <= "
            "(SELECT created_at FROM jobs WHERE id = ?)",
            (job_id,),
        )
        position = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'")
        total = (await cursor.fetchone())[0]
    finally:
        await db.close()
    return {"position": position, "total": total}


@jobs_bp.route("/jobs/<job_id>/stream")
@login_required
async def job_stream(job_id):
    user_id = None if session.get("is_admin") else session["user_id"]
    job = await _load_job_from_db(job_id, user_id)
    if not job:
        abort(404)

    async def generate():
        q = job.logger.subscribe()
        try:
            # Send existing messages first
            for msg in job.logger.messages:
                yield f"data: {json.dumps(msg)}\n\n"

            # If job is already finished, send final status and stop
            if job.status in ("done", "failed"):
                yield f"data: {json.dumps({'level': 'STATUS', 'msg': job.status})}\n\n"
                return

            # Stream new messages
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("level") == "STATUS" and msg.get("msg") in (
                        "done",
                        "failed",
                    ):
                        break
                except asyncio.TimeoutError:
                    # Check DB for status changes (worker API may be in another process)
                    db = await get_db()
                    try:
                        cursor = await db.execute(
                            "SELECT status FROM jobs WHERE id = ?", (job_id,)
                        )
                        row = await cursor.fetchone()
                    finally:
                        await db.close()
                    if row and row["status"] in ("done", "failed"):
                        job.status = row["status"]
                        yield f"data: {json.dumps({'level': 'STATUS', 'msg': row['status']})}\n\n"
                        break
                    yield ": keepalive\n\n"
        finally:
            job.logger.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@jobs_bp.route("/jobs/<job_id>/download")
@login_required
async def job_download(job_id):
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)
    if not job.result_path or not os.path.exists(job.result_path):
        abort(404)

    # Extract SFO fields from result zip for filename and PS4 structure
    sfo = _extract_sfo_fields_from_zip(job.result_path)
    title_id = (job.params.get("title_id", "") if job.params else "") or sfo.get(
        "TITLE_ID", ""
    )

    # Build download filename: <Title>_<TITLE_ID>_dec/enc.zip
    op_suffix = {"decrypt": "dec", "encrypt": "enc"}.get(job.operation, job.operation)
    game_title = ""
    if title_id:
        game_title = await lookup_title(title_id) or ""
    if game_title and title_id:
        safe_title = re.sub(r"[^\w\s\-]", "", game_title).strip().replace(" ", "_")
        filename = f"{safe_title}_{title_id}_{op_suffix}.zip"
    elif title_id:
        filename = f"{title_id}_{op_suffix}.zip"
    else:
        filename = f"{job.operation}_{job_id[:8]}.zip"

    # For PS4 encrypt/resign, restructure zip as PS4/SAVEDATA/<account_id>/<title_id>/
    platform = job.params.get("platform", "ps4") if job.params else "ps4"
    if platform == "ps4" and job.operation in ("encrypt", "resign") and job.params:
        account_id = job.params.get("account_id", "")
        if account_id and title_id:
            structured_path = job.result_path.replace(".zip", "_ps4.zip")
            try:
                _restructure_ps4_zip(
                    job.result_path, structured_path, account_id, title_id
                )
                return await send_file(
                    structured_path, as_attachment=True, attachment_filename=filename
                )
            except Exception:
                pass  # Fall through to serve original zip

    # Sanitize filenames inside zip for Windows compatibility
    serve_path = job.result_path
    if _zip_needs_sanitizing(job.result_path):
        sanitized_path = job.result_path.replace(".zip", "_safe.zip")
        try:
            _sanitize_result_zip(job.result_path, sanitized_path)
            serve_path = sanitized_path
        except Exception:
            pass

    return await send_file(serve_path, as_attachment=True, attachment_filename=filename)


def _extract_sfo_fields_from_zip(zip_path):
    """Read TITLE_ID and SAVEDATA_DIRECTORY from param.sfo inside a result zip."""
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
                            if (
                                key in ("TITLE_ID", "SAVEDATA_DIRECTORY")
                                and fmt == 0x0204
                            ):
                                result[key] = (
                                    data[data_off + d_off : data_off + d_off + d_len]
                                    .rstrip(b"\x00")
                                    .decode()
                                )
                    break
    except Exception:
        pass
    return result


def _sanitize_zip_filename(name):
    """Replace characters illegal on Windows filesystems: \\ / : * ? \" < > |"""
    # Sanitize each path component but preserve directory separators
    parts = name.replace("\\", "/").split("/")
    sanitized = []
    for part in parts:
        part = re.sub(r'[:<>"|?*]', "_", part)
        sanitized.append(part)
    return "/".join(sanitized)


def _sanitize_result_zip(src_zip, dst_zip):
    """Rewrite zip with sanitized filenames for Windows compatibility."""
    with (
        zipfile.ZipFile(src_zip, "r") as zin,
        zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout,
    ):
        for info in zin.infolist():
            if info.is_dir():
                continue
            data = zin.read(info.filename)
            zout.writestr(_sanitize_zip_filename(info.filename), data)


def _zip_needs_sanitizing(zip_path):
    """Check if any filename in the zip contains Windows-illegal characters."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if re.search(r'[:<>"|?*]', name):
                    return True
    except Exception:
        pass
    return False


def _restructure_ps4_zip(src_zip, dst_zip, account_id, title_id):
    """Repack zip with PS4 USB structure: PS4/SAVEDATA/<account_id>/<title_id>/"""
    # account_id is stored big-endian, USB folder needs little-endian
    if len(account_id) == 16:
        usb_id = "".join(reversed([account_id[i : i + 2] for i in range(0, 16, 2)]))
    else:
        usb_id = account_id
    prefix = f"PS4/SAVEDATA/{usb_id}/{title_id}/"
    with (
        zipfile.ZipFile(src_zip, "r") as zin,
        zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout,
    ):
        for info in zin.infolist():
            if info.is_dir():
                continue
            data = zin.read(info.filename)
            zout.writestr(prefix + _sanitize_zip_filename(info.filename), data)


@jobs_bp.route("/jobs/<job_id>/files")
@login_required
async def job_files(job_id):
    """List decrypted files for encrypt phase 2."""
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)

    return {"files": job.file_list or [], "status": job.status}
