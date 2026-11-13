import hmac
import json
import os
import re
import shutil
import uuid
import zipfile
from functools import wraps

# Minimum garlic-worker version required to pull ANY job. v1.1.5 and below
# ran with default umask (022), which stripped the world-write bit from every
# file written into a mounted PFS save (0777 requested, 0775 landed). The
# game reads the save back and flags 0775 files as corrupt. Fixed in v1.1.6
# via umask(0) + explicit fchmod(0777) in copy_dir_pfs/copy_dir_recursive.
# This gate also covers the older v1.1.2 SFO_AID_OFFSET fix (PS4-only).
# Outdated workers polling /next get 426 until they update.
MIN_WORKER_VERSION = (1, 5, 9)


def parse_worker_version(ua: str):
    """Pull (major, minor, patch) from sonicloader-worker/vX.Y.Z UA. None if absent."""
    m = re.search(r"worker/v?(\d+)\.(\d+)\.(\d+)", ua or "")
    return tuple(int(x) for x in m.groups()) if m else None


from quart import Blueprint, Response, abort, request, send_file

from config import CHUNK_DIR, WORKER_KEY
from models import get_db
from services.jobs import get_or_create_job_logger, push_log
from services.rate_limit import enforce_job_rate_limit
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


def _client_ip():
    """Return the real client IP. Prefers CF-Connecting-IP (Cloudflare),
    then X-Real-IP / X-Forwarded-For (set by nginx), then peer."""
    cf = request.headers.get("CF-Connecting-IP", "").strip()
    if cf:
        return cf
    real = request.headers.get("X-Real-IP", "").strip()
    if real:
        return real
    fwd = request.headers.get("X-Forwarded-For", "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


async def _touch_instance(db, worker_key_id, platform):
    """Upsert a worker_instances row for this (key_id, ip) and bump heartbeat.

    Also sticky-marks the is_psX_capable flag for whichever platform this poll
    requested. A dual-platform worker (PS5 ELF that polls both ps5 and ps4)
    accumulates both flags and stays counted in both pools even between polls.
    """
    if not worker_key_id:
        return
    ip = _client_ip()
    if not ip:
        return
    ua = request.headers.get("User-Agent", "")[:200]
    ps4_cap = 1 if platform == "ps4" else 0
    ps5_cap = 1 if platform == "ps5" else 0
    # Set online_since when this instance has been quiet for >300s
    await db.execute(
        "INSERT INTO worker_instances (worker_key_id, ip, user_agent, last_platform, "
        "  is_ps4_capable, is_ps5_capable, online_since) "
        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(worker_key_id, ip) DO UPDATE SET "
        "  last_seen = CURRENT_TIMESTAMP, "
        "  user_agent = excluded.user_agent, "
        "  last_platform = excluded.last_platform, "
        "  is_ps4_capable = MAX(is_ps4_capable, excluded.is_ps4_capable), "
        "  is_ps5_capable = MAX(is_ps5_capable, excluded.is_ps5_capable), "
        "  online_since = CASE "
        "    WHEN last_seen IS NULL OR last_seen < datetime('now', '-300 seconds') "
        "      THEN CURRENT_TIMESTAMP ELSE online_since END",
        (worker_key_id, ip, ua, platform, ps4_cap, ps5_cap),
    )


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


async def get_worker_api_access(key):
    """Return API submission permissions for an active worker key."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, api_enabled, ppr_derive_enabled FROM worker_keys "
            "WHERE key = ? AND is_active = 1",
            (key,),
        )
        return await cursor.fetchone()
    finally:
        await db.close()


async def _ensure_global_key_in_db():
    """Insert or update the global worker key in the DB. Returns its row id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM worker_keys WHERE key = ?", (WORKER_KEY,)
        )
        row = await cursor.fetchone()
        if row:
            key_id = row["id"]
            await db.execute(
                "UPDATE worker_keys SET last_used = CURRENT_TIMESTAMP WHERE id = ?",
                (key_id,),
            )
        else:
            cursor = await db.execute(
                "INSERT INTO worker_keys (user_id, key, name, is_active, last_used) "
                "VALUES (NULL, ?, 'global', 1, CURRENT_TIMESTAMP)",
                (WORKER_KEY,),
            )
            key_id = cursor.lastrowid
        await db.commit()
        return key_id
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


async def _resolve_key_id(db, key):
    """Look up worker_keys.id for a given key string."""
    if not key:
        return None
    cursor = await db.execute("SELECT id FROM worker_keys WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["id"] if row else None


@api_bp.route("/next", methods=["GET"])
@require_worker_key
async def next_job():
    """Return the next queued job, or 204 if none.
    Workers can pass ?platform=ps5 to only receive jobs for that platform."""
    worker_platform = request.args.get("platform", "ps4")

    # Block outdated workers from pulling ANY job. See MIN_WORKER_VERSION
    # comment for the umask/chmod corruption fixed in v1.1.6.
    ver = parse_worker_version(request.headers.get("User-Agent", ""))
    if ver is None or ver < MIN_WORKER_VERSION:
        min_str = ".".join(str(x) for x in MIN_WORKER_VERSION)
        cur_str = ".".join(str(x) for x in ver) if ver else "unknown"
        return Response(
            json.dumps(
                {
                    "error": "outdated_worker",
                    "min_version": min_str,
                    "detected_version": cur_str,
                    "message": (
                        f"garlic-worker {cur_str} produces corrupt saves — "
                        f"v{min_str}+ required (PFS chmod 0775 bug). "
                        f"Update via elf-arsenal or rebuild from "
                        f"git.etawen.dev/soniciso/elf-arsenal."
                    ),
                }
            ),
            status=426,
            content_type="application/json",
        )

    worker_key = request.headers.get("X-Worker-Key", "")
    _, retry_after = await enforce_job_rate_limit(worker_key)
    if retry_after:
        return Response(
            json.dumps({"error": "rate limit exceeded; try again later"}),
            status=429,
            content_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )

    # Track worker platform on heartbeat
    if worker_key:
        db = await get_db()
        try:
            # Check key-level suspension (legacy / user-owned keys)
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

            # Check instance-level suspension (per-IP — important for the
            # shared global key so one bad PS5 doesn't lock out everyone).
            client_ip = _client_ip()
            if client_ip:
                cursor = await db.execute(
                    "SELECT wi.suspended_until FROM worker_instances wi "
                    "JOIN worker_keys wk ON wk.id = wi.worker_key_id "
                    "WHERE wk.key = ? AND wi.ip = ?",
                    (worker_key, client_ip),
                )
                wi = await cursor.fetchone()
                if wi and wi["suspended_until"]:
                    cursor2 = await db.execute(
                        "SELECT ? > datetime('now') as is_suspended",
                        (wi["suspended_until"],),
                    )
                    check = await cursor2.fetchone()
                    if check and check["is_suspended"]:
                        return Response(
                            json.dumps(
                                {
                                    "suspended_until": wi["suspended_until"],
                                    "scope": "instance",
                                }
                            ),
                            status=429,
                            content_type="application/json",
                        )
                    else:
                        await db.execute(
                            "UPDATE worker_instances SET suspended_until = NULL "
                            "WHERE id = (SELECT wi2.id FROM worker_instances wi2 "
                            "  JOIN worker_keys wk ON wk.id = wi2.worker_key_id "
                            "  WHERE wk.key = ? AND wi2.ip = ?)",
                            (worker_key, client_ip),
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
            # Per-instance (per-IP) heartbeat
            key_id = await _resolve_key_id(db, worker_key)
            await _touch_instance(db, key_id, worker_platform)
            await db.commit()
        finally:
            await db.close()

    db = await get_db()
    try:
        # Stale-worker requeue + slow-worker penalty.
        # 1. Find running jobs whose assigned worker hasn't heartbeated in >300s.
        # 2. Bump each abandoning instance's jobs_failed counter.
        # 3. If the same instance has been requeued 3+ times in the last hour,
        #    short-suspend it (15 min) — probably slow-uploader on flaky net.
        # 4. Then requeue the jobs.
        stale_cursor = await db.execute(
            """
            SELECT DISTINCT j.worker_instance_id
            FROM jobs j
            JOIN worker_instances wi ON wi.id = j.worker_instance_id
            WHERE j.status = 'running'
              AND j.worker_instance_id IS NOT NULL
              AND (wi.last_seen IS NULL OR wi.last_seen < datetime('now', '-300 seconds'))
            """
        )
        stale_ids = [r[0] for r in await stale_cursor.fetchall()]

        for iid in stale_ids:
            # Bump failure counter (so admin dashboard shows it)
            await db.execute(
                "UPDATE worker_instances SET jobs_failed = jobs_failed + 1 WHERE id = ?",
                (iid,),
            )
            # Count abandonments in the last hour for this instance
            ac = await db.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE worker_instance_id = ?
                  AND retry_count > 0
                  AND started_at > datetime('now', '-1 hour')
                """,
                (iid,),
            )
            count = (await ac.fetchone())[0]
            if count >= 3:
                # Never suspend the localhost worker — under monopoly mode
                # it is the sole PS4 processor; a burst of failures
                # would otherwise wedge the queue for 15 min.
                await db.execute(
                    "UPDATE worker_instances SET suspended_until = datetime('now', '+15 minutes') "
                    "WHERE id = ? AND ip NOT IN ('127.0.0.1','::1') "
                    "AND (suspended_until IS NULL OR suspended_until < datetime('now'))",
                    (iid,),
                )

        # Now requeue the jobs
        await db.execute(
            """
            UPDATE jobs SET status = 'queued',
                            worker_key_id = NULL,
                            worker_instance_id = NULL,
                            retry_count = COALESCE(retry_count, 0) + 1
            WHERE status = 'running'
              AND worker_instance_id IS NOT NULL
              AND worker_instance_id IN (
                SELECT id FROM worker_instances
                WHERE last_seen IS NULL OR last_seen < datetime('now', '-300 seconds')
              )
            """
        )
        await db.commit()

        # When the monopoly flag exists, all PS4 jobs are routed to the
        # local bridge worker. Remote PS4 workers still heartbeat
        # (staying counted as online) but only receive PS5 jobs.
        if os.path.exists("/opt/htos/bridge_monopoly") and worker_platform == "ps4":
            client_ip_now = _client_ip()
            if not (client_ip_now.startswith("127.") or client_ip_now == "::1"):
                return Response(status=204)

        # Atomic claim: pick oldest queued job matching this worker's platform
        # and flip it to 'running' in a single UPDATE. Prevents two workers
        # from grabbing the same job when they poll simultaneously. Platform
        # match: exact worker_platform, OR NULL/unknown (legacy = ps4).
        legacy_ok = 1 if worker_platform == "ps4" else 0
        cursor = await db.execute(
            """
            UPDATE jobs SET status = 'running'
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'queued'
                  AND (
                    json_extract(params, '$.platform') = ?
                    OR (? = 1 AND (
                        json_extract(params, '$.platform') IS NULL
                        OR json_extract(params, '$.platform') = 'unknown'
                    ))
                  )
                ORDER BY created_at ASC LIMIT 1
            )
            RETURNING id, user_id, operation, params, created_at
            """,
            (worker_platform, legacy_ok),
        )
        row = await cursor.fetchone()
        await db.commit()
        if not row:
            return Response(status=204)
        job = dict(row)
        job["params"] = json.loads(job["params"]) if job["params"] else {}
        return job
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
            # Strip macOS zip cruft — otherwise worker sees __MACOSX as a
            # second top-level dir and descend_single_dir bails, hiding
            # sce_sys/ from the encrypt path (was a real user report).
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for f in files:
                if f == ".DS_Store" or f.startswith("._"):
                    continue
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, upload_dir)
                zf.write(filepath, arcname)

    # Manual async streaming — Quart's send_file wraps the file in an
    # aio helper that reads to memory before yielding on multi-GB responses,
    # causing hypercorn to drop the connection ~1.7 GB in. A plain async
    # generator + explicit Content-Length reads chunked from disk.
    import aiofiles

    size = os.path.getsize(tmp_path)

    async def _stream():
        async with aiofiles.open(tmp_path, "rb") as fh:
            while True:
                chunk = await fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return Response(
        _stream(),
        mimetype="application/zip",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{job_id}.zip"',
        },
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

    # Failure-retry: a worker that fails a job has the job requeued (status
    # back to "queued") up to MAX_RETRIES times before we finally mark it
    # failed. Lets another worker pick it up. Inject a user-facing apology
    # line into jobs.logs so the live status page surfaces the retry.
    MAX_RETRIES = 3
    requeued = False
    apology_line = None
    if status == "failed":
        db_chk = await get_db()
        try:
            cursor = await db_chk.execute(
                "SELECT retry_count FROM jobs WHERE id = ?", (job_id,)
            )
            r = await cursor.fetchone()
            cur_retries = (r["retry_count"] or 0) if r else 0
            if cur_retries < MAX_RETRIES:
                status = "queued"  # override — go back into the pool
                requeued = True
                apology_line = (
                    f"Sorry, this is taking a bit — finding a new worker "
                    f"(attempt {cur_retries + 2}/{MAX_RETRIES + 1})..."
                )
        finally:
            await db_chk.close()

    db = await get_db()
    try:
        fields = ["status = ?"]
        values = [status]
        if "error" in data and not requeued:
            fields.append("error = ?")
            values.append(data["error"])
        if requeued:
            # Clear stale worker stamps so a different worker can pick it up
            # cleanly, and bump the retry counter.
            fields.append("retry_count = retry_count + 1")
            fields.append("worker_key_id = NULL")
            fields.append("worker_instance_id = NULL")
        if "result_path" in data:
            # Validate result_path stays within workspace
            rp = data["result_path"]
            allowed_prefix = os.path.realpath("workspace")
            if os.path.realpath(rp).startswith(allowed_prefix + os.sep):
                fields.append("result_path = ?")
                values.append(rp)
        # Per-job duration timestamps for passive worker-speed measurement.
        # started_at gets stamped on the first running transition (or refreshed
        # when a requeued job restarts on a different worker — we want the
        # current attempt's elapsed time, not the original). completed_at on
        # the terminal status.
        if status == "running":
            fields.append("started_at = CURRENT_TIMESTAMP")
        elif status in ("done", "failed") and not requeued:
            fields.append("completed_at = CURRENT_TIMESTAMP")

        # Track which worker handled this job
        key_id = await _resolve_key_id(db, worker_key) if worker_key else None
        if status == "running" and key_id:
            fields.append("worker_key_id = ?")
            values.append(key_id)
            # Stamp the originating instance (per-IP) at run time
            ip = _client_ip()
            if ip:
                cursor = await db.execute(
                    "SELECT id FROM worker_instances WHERE worker_key_id = ? AND ip = ?",
                    (key_id, ip),
                )
                inst = await cursor.fetchone()
                if inst:
                    fields.append("worker_instance_id = ?")
                    values.append(inst["id"])
        values.append(job_id)
        await db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)

        # On requeue, append a WARN line to jobs.logs so the live SSE feed
        # surfaces it AND push to the in-process log pubsub for live viewers.
        if requeued and apology_line:
            entry = json.dumps({"level": "WARN", "msg": apology_line})
            await db.execute(
                "UPDATE jobs SET logs = CASE WHEN logs IS NULL THEN ? "
                "ELSE logs || '\n' || ? END WHERE id = ?",
                (entry, entry, job_id),
            )
            try:
                push_log(job_id, "WARN", apology_line)
            except Exception:
                pass
        # Increment jobs_completed counter for this worker key
        if status == "done" and worker_key:
            await db.execute(
                "UPDATE worker_keys SET jobs_completed = jobs_completed + 1 WHERE key = ?",
                (worker_key,),
            )
        # Per-instance counters (use the instance recorded on the job, if any)
        if status in ("done", "failed"):
            cursor = await db.execute(
                "SELECT worker_instance_id FROM jobs WHERE id = ?", (job_id,)
            )
            jrow = await cursor.fetchone()
            inst_id = jrow["worker_instance_id"] if jrow else None
            if inst_id:
                col = "jobs_done" if status == "done" else "jobs_failed"
                await db.execute(
                    f"UPDATE worker_instances SET {col} = {col} + 1 WHERE id = ?",
                    (inst_id,),
                )
        # Auto-suspend after 10 consecutive failures.
        # Prefer per-instance suspension (isolates one buggy PS5 on the shared
        # global key); fall back to per-key only when no instance row is
        # associated with the failing job (legacy / pre-instance tracking).
        if status == "failed":
            if inst_id:
                cursor3 = await db.execute(
                    "SELECT status FROM jobs WHERE worker_instance_id = ? "
                    "ORDER BY created_at DESC LIMIT 10",
                    (inst_id,),
                )
                recent = await cursor3.fetchall()
                if len(recent) >= 10 and all(r["status"] == "failed" for r in recent):
                    # Same bridge exemption as the +15 min trigger above.
                    await db.execute(
                        "UPDATE worker_instances SET suspended_until = "
                        "datetime('now', '+24 hours') "
                        "WHERE id = ? AND ip NOT IN ('127.0.0.1','::1')",
                        (inst_id,),
                    )
            elif worker_key:
                cursor = await db.execute(
                    "SELECT id FROM worker_keys WHERE key = ?", (worker_key,)
                )
                wk = await cursor.fetchone()
                if wk:
                    cursor2 = await db.execute(
                        "SELECT status FROM jobs WHERE worker_key_id = ? "
                        "AND worker_instance_id IS NULL "
                        "ORDER BY created_at DESC LIMIT 10",
                        (wk["id"],),
                    )
                    recent = await cursor2.fetchall()
                    if len(recent) >= 10 and all(
                        r["status"] == "failed" for r in recent
                    ):
                        await db.execute(
                            "UPDATE worker_keys SET suspended_until = "
                            "datetime('now', '+24 hours') WHERE id = ?",
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

            finally:
                await db2.close()
        except Exception:
            pass

    # BO1 chain: when a decrypt job carrying bo1_patch completes, extract
    # its result zip, overwrite .PRF files with the encrypted patch blob,
    # and queue a follow-up encrypt job that re-signs to the target account.
    if status == "done":
        try:
            db_bo1 = await get_db()
            try:
                cursor = await db_bo1.execute(
                    "SELECT operation, result_path, params, user_id FROM jobs WHERE id = ?",
                    (job_id,),
                )
                jrow = await cursor.fetchone()
            finally:
                await db_bo1.close()
            if jrow and jrow["operation"] == "decrypt" and jrow["result_path"]:
                jp = json.loads(jrow["params"] or "{}")
                if jp.get("bo1_patch") and jp.get("bo1_account_id"):
                    from services.bo1 import chain_after_decrypt

                    await chain_after_decrypt(
                        decrypt_job_id=job_id,
                        user_id=jrow["user_id"],
                        result_zip=jrow["result_path"],
                        patch_text=jp["bo1_patch"],
                        target_account_id=jp["bo1_account_id"],
                    )
                elif jp.get("bo1_editor"):
                    from services.bo1 import chain_stage_only

                    await chain_stage_only(
                        decrypt_job_id=job_id,
                        user_id=jrow["user_id"],
                        result_zip=jrow["result_path"],
                    )
        except Exception as e:
            push_log(job_id, "ERROR", f"BO1 chain failed: {e}")

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

    db = await get_db()
    try:
        cursor = await db.execute("SELECT operation FROM jobs WHERE id = ?", (job_id,))
        operation_row = await cursor.fetchone()
    finally:
        await db.close()
    operation = operation_row["operation"] if operation_row else ""

    result_dir = os.path.join("workspace", "results")
    os.makedirs(result_dir, exist_ok=True)
    extension = "json" if operation == "ppr_derive" else "zip"
    result_path = os.path.join(result_dir, f"{job_id}.{extension}")

    with open(result_path, "wb") as out:
        for part in parts:
            part_path = os.path.join(chunk_dir, part)
            with open(part_path, "rb") as inp:
                shutil.copyfileobj(inp, out)

    # Clean up chunk dir
    shutil.rmtree(chunk_dir, ignore_errors=True)

    if operation != "ppr_derive":
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


# ---------------------------------------------------------------------------
# Self-update: outdated workers download their replacement from here.
# The 426 response from /next tells the worker its version is too low; the
# worker then GETs this endpoint, streams the reply into local elfldr on
# 127.0.0.1:9021, and kill-9s itself. The new ELF elfldr loaded takes over.
#
# We proxy from git.etawen.dev/earthonion/garlic-worker/releases/latest so
# there is a single source of truth. A short local disk cache keeps the
# upstream API happy on burst-restart cycles.
# ---------------------------------------------------------------------------

import time as _time
import urllib.error as _urllib_error
import urllib.request as _urllib_request

_UPDATE_CACHE_DIR = "/var/cache/htos/worker-update"
_UPDATE_CACHE_TTL = 300  # 5 minutes


def _cached_worker_elf(platform):
    """Return path to a fresh copy of garlic-worker-{platform}.elf.
    Downloads from the latest release if the cache is missing/stale."""
    assert platform in ("ps4", "ps5")
    os.makedirs(_UPDATE_CACHE_DIR, exist_ok=True)
    fname = f"garlic-worker-{platform}.elf"
    dest = os.path.join(_UPDATE_CACHE_DIR, fname)
    try:
        mtime = os.path.getmtime(dest)
        if _time.time() - mtime < _UPDATE_CACHE_TTL and os.path.getsize(dest) > 4096:
            return dest
    except FileNotFoundError:
        pass

    # Forgejo has no /releases/latest/download/ shortcut - hit the API for
    # the latest release, then download the matching asset URL from it.
    api_url = (
        "https://git.etawen.dev/api/v1/repos/earthonion/garlic-worker/releases?limit=1"
    )
    api_req = _urllib_request.Request(
        api_url, headers={"User-Agent": "htos-worker-update-proxy"}
    )
    with _urllib_request.urlopen(api_req, timeout=15) as r:
        rel = json.loads(r.read())[0]
    asset_url = None
    for a in rel.get("assets", []):
        if a.get("name") == fname:
            asset_url = a["browser_download_url"]
            break
    if not asset_url:
        raise _urllib_error.URLError(
            f"asset {fname} not in latest release {rel.get('tag_name')}"
        )
    tmp = dest + ".tmp"
    req = _urllib_request.Request(
        asset_url, headers={"User-Agent": "htos-worker-update-proxy"}
    )
    with _urllib_request.urlopen(req, timeout=30) as r:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=64 * 1024)
    os.replace(tmp, dest)
    return dest


@api_bp.route("/update", methods=["GET"])
@require_worker_key
async def worker_update():
    """Stream the latest garlic-worker ELF for the requested platform.
    Triggered by workers that received 426 from /next."""
    platform = request.args.get("platform", "ps5").lower()
    if platform not in ("ps4", "ps5"):
        return {"error": "bad_platform"}, 400
    try:
        path = _cached_worker_elf(platform)
    except (_urllib_error.URLError, OSError) as e:
        return {"error": "upstream_unavailable", "detail": str(e)}, 502
    return await send_file(
        path,
        mimetype="application/octet-stream",
        attachment_filename=f"garlic-worker-{platform}.elf",
    )
