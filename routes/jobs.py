import asyncio
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

from quart import Blueprint, Response, abort, redirect, render_template, send_file, session, url_for

from auth import login_required
from models import get_db
from services.files import extract_account_id_from_zip
from services.jobs import get_or_create_job_logger
from services.titles import lookup_title
from config_throttles import throttle_delay
from utils.extras import group_supporters

jobs_bp = Blueprint("jobs", __name__)


async def _load_job_from_db(job_id, user_id):
    """Load job from DB and ensure it's in the in-memory registry for SSE."""
    # Always check DB for latest state (workers may run in separate processes)
    db = await get_db()
    try:
        if user_id is None:
            cursor = await db.execute(
                "SELECT id, user_id, operation, status, result_path, error, params, logs, completed_at FROM jobs WHERE id = ?",
                (job_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, operation, status, result_path, error, params, logs, completed_at FROM jobs WHERE id = ? AND user_id = ?",
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
    job.completed_at = row["completed_at"] if "completed_at" in row.keys() else None
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




def _throttle_available_at(job, sfo=None):
    """Return the datetime the download unlocks, or None if not throttled.

    Reads config_throttles.throttle_delay for the (title_id, operation) pair.
    title_id comes from job.params if present, else from the SFO extracted
    from the result zip (passed by the caller to avoid double-extract).
    """
    if not getattr(job, "completed_at", None):
        return None
    tid = (job.params.get("title_id", "") if job.params else "") or ((sfo or {}).get("TITLE_ID", ""))
    delay = throttle_delay(tid, job.operation or "")
    if delay <= 0:
        return None
    completed = job.completed_at
    if isinstance(completed, str):
        try:
            completed = datetime.strptime(completed, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                completed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            except ValueError:
                return None
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return completed + timedelta(seconds=delay)


@jobs_bp.route("/jobs/<job_id>")
@login_required
async def job_status(job_id):
    user_id = None if session.get("is_admin") else session["user_id"]
    job = await _load_job_from_db(job_id, user_id)
    if not job:
        abort(404)

    # BO1 chain: if this decrypt job spawned a follow-up encrypt job, hop the
    # user over to it once the decrypt is done. The SSE handler triggers
    # location.reload() on status=done, so users land here right when the
    # follow-up exists.
    if job.status == "done" and job.params and job.params.get("bo1_encrypt_job"):
        return redirect(url_for("jobs.job_status", job_id=job.params["bo1_encrypt_job"]))

    # BO1 editor mode: decrypt just finished, stage dir was populated by the
    # chain hook — jump the user into the PRF browser.
    if job.status == "done" and job.params and job.params.get("bo1_editor"):
        return redirect(url_for("bo1.bo1_edit", job_id=job_id))

    # Throttle: if a per-(title_id, op) delay is in force and hasn't
    # elapsed, present the job as still running so the user sees no
    # download UI and no signal that anything different is happening.
    _throttle_pending = False
    if job.status == "done":
        _avail = _throttle_available_at(job)
        if _avail:
            from datetime import datetime as _dt, timezone as _tz
            if _avail > _dt.now(_tz.utc):
                _throttle_pending = True
                job.status = "running"

    # Extract account ID and title from result zip if not already in params
    if job.status == "done" and job.result_path and os.path.exists(job.result_path) and not _throttle_pending:
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

    supporters = []
    anonymous_count = 0
    if job.status == "done":
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT from_name, is_subscription_payment, tier_name "
                "FROM kofi_supporters "
                "WHERE LOWER(COALESCE(from_name, \"\")) != \"anonymous\" "
                "ORDER BY created_at DESC LIMIT 40"
            )
            supporters = group_supporters([dict(r) for r in await cursor.fetchall()])
            cursor = await db.execute(
                "SELECT COUNT(*) FROM kofi_supporters "
                "WHERE LOWER(COALESCE(from_name, \"\")) = \"anonymous\""
            )
            anonymous_count = (await cursor.fetchone())[0]
        finally:
            await db.close()

    return await render_template(
        "job_status.html",
        job=job,
        queue_position=queue_position,
        queue_total=queue_total,
        supporters=supporters,
        anonymous_count=anonymous_count,
        throttle_pending=_throttle_pending,
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
        db_log_count = len(job.logger.messages)
        try:
            # Send existing messages first
            for msg in job.logger.messages:
                yield f"data: {json.dumps(msg)}\n\n"

            # If job is already finished, send final status and stop.
            # Throttled done jobs get presented as still-running and the SSE
            # connection is held open (keepalives) until unlock time.
            if job.status == "done":
                _avail = _throttle_available_at(job)
                if _avail:
                    _now = datetime.now(timezone.utc)
                    if _avail > _now:
                        yield f"data: {json.dumps({'level': 'STATUS', 'msg': 'running'})}\n\n"
                        _wait = int((_avail - _now).total_seconds())
                        for _ in range(_wait):
                            await asyncio.sleep(1)
                            yield ": keepalive\n\n"
                        yield f"data: {json.dumps({'level': 'STATUS', 'msg': 'done'})}\n\n"
                        return
            if job.status in ("done", "failed"):
                yield f"data: {json.dumps({'level': 'STATUS', 'msg': job.status})}\n\n"
                return

            # Stream new messages
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=3)
                    yield f"data: {json.dumps(msg)}\n\n"
                    db_log_count += 1
                    if msg.get("level") == "STATUS" and msg.get("msg") in (
                        "done",
                        "failed",
                    ):
                        break
                except asyncio.TimeoutError:
                    # Poll DB for status and new logs (worker may be in another process)
                    db = await get_db()
                    try:
                        cursor = await db.execute(
                            "SELECT status, logs FROM jobs WHERE id = ?", (job_id,)
                        )
                        row = await cursor.fetchone()
                    finally:
                        await db.close()
                    if row and row["logs"]:
                        db_entries = row["logs"].split("\n")
                        if len(db_entries) > db_log_count:
                            for line in db_entries[db_log_count:]:
                                try:
                                    entry = json.loads(line)
                                    yield f"data: {json.dumps(entry)}\n\n"
                                except (json.JSONDecodeError, ValueError):
                                    pass
                            db_log_count = len(db_entries)
                    if row and row["status"] in ("done", "failed"):
                        job.status = row["status"]
                        if row["status"] == "done":
                            _dbc = await get_db()
                            try:
                                _cc = await _dbc.execute(
                                    "SELECT completed_at FROM jobs WHERE id = ?", (job_id,)
                                )
                                _rr = await _cc.fetchone()
                                if _rr:
                                    job.completed_at = _rr["completed_at"]
                            finally:
                                await _dbc.close()
                            _avail = _throttle_available_at(job)
                            if _avail:
                                _now = datetime.now(timezone.utc)
                                if _avail > _now:
                                    yield f"data: {json.dumps({'level': 'STATUS', 'msg': 'running'})}\n\n"
                                    _wait = int((_avail - _now).total_seconds())
                                    for _ in range(_wait):
                                        await asyncio.sleep(1)
                                        yield ": keepalive\n\n"
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



def _xaccel(path, filename):
    """Return a Response that hands the download off to nginx via
    X-Accel-Redirect. Backing files live under
    /opt/htos/workspace/results/ and are served by the internal
    location /_result_files/ in the nginx config."""
    from quart import Response
    resp = Response(b"", status=200)
    resp.headers["X-Accel-Redirect"] = f"/_result_files/{os.path.basename(path)}"
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@jobs_bp.route("/jobs/<job_id>/download")
@login_required
async def job_download(job_id):
    # Admins can pull any users result; regular users only their own
    user_id = None if session.get("is_admin") else session["user_id"]
    job = await _load_job_from_db(job_id, user_id)
    if not job:
        abort(404)
    if not job.result_path or not os.path.exists(job.result_path):
        abort(404)

    # Extract SFO fields from result zip for filename and PS4 structure
    sfo = _extract_sfo_fields_from_zip(job.result_path)

    # Throttle: (title_id, operation) may be gated behind a download-delay.
    # Admins bypass. Everyone else 404s silently to match the fake
    # still-running narrative the job status page presents; no hint that
    # the result exists and is being withheld.
    if not session.get("is_admin"):
        avail = _throttle_available_at(job, sfo=sfo)
        if avail and avail > datetime.now(timezone.utc):
            abort(404)
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
                return _xaccel(structured_path, filename)
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

    return _xaccel(serve_path, filename)


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
    """Rewrite zip with sanitized filenames for Windows compatibility.

    Same atomic-write pattern as _restructure_ps4_zip — concurrent
    /download calls must never truncate dst mid-serve.
    """
    if os.path.exists(dst_zip) and os.path.getmtime(dst_zip) >= os.path.getmtime(src_zip):
        return
    tmp_zip = f"{dst_zip}.tmp.{os.getpid()}"
    try:
        with (
            zipfile.ZipFile(src_zip, "r") as zin,
            zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout,
        ):
            for info in zin.infolist():
                if info.is_dir():
                    continue
                data = zin.read(info.filename)
                zout.writestr(_sanitize_zip_filename(info.filename), data)
        os.replace(tmp_zip, dst_zip)
    except Exception:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        raise


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
    """Repack zip with PS4 USB structure: PS4/SAVEDATA/<account_id>/<title_id>/

    Writes to a temp file and atomically renames into place, so a concurrent
    download call (retry from the same user, or nginx still mid-serve) never
    sees a truncated dst — the second caller replaces the inode; nginx's
    open fd on the old inode keeps working until it closes.
    """
    if os.path.exists(dst_zip) and os.path.getmtime(dst_zip) >= os.path.getmtime(src_zip):
        return  # already fresh
    if len(account_id) == 16:
        usb_id = "".join(reversed([account_id[i : i + 2] for i in range(0, 16, 2)]))
    else:
        usb_id = account_id
    prefix = f"PS4/SAVEDATA/{usb_id}/{title_id}/"
    tmp_zip = f"{dst_zip}.tmp.{os.getpid()}"
    try:
        with (
            zipfile.ZipFile(src_zip, "r") as zin,
            zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout,
        ):
            for info in zin.infolist():
                if info.is_dir():
                    continue
                data = zin.read(info.filename)
                zout.writestr(prefix + _sanitize_zip_filename(info.filename), data)
        os.replace(tmp_zip, dst_zip)
    except Exception:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        raise


@jobs_bp.route("/jobs/<job_id>/files")
@login_required
async def job_files(job_id):
    """List decrypted files for encrypt phase 2."""
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)

    return {"files": job.file_list or [], "status": job.status}
