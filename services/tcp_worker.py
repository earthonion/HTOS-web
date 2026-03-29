"""TCP direct-connect worker protocol.

Workers connect to a dedicated TCP port (bypassing Cloudflare).
The server pushes jobs immediately — no polling.
File transfers happen over the raw TCP connection — no chunking needed.

Protocol: length-prefixed messages.
  [4-byte big-endian length][JSON payload]
  For binary transfers: a JSON message with 'size' precedes raw bytes.
"""

import asyncio
import hmac
import json
import logging
import os
import struct
import zipfile

from config import WORKER_KEY
from models import get_db
from services.jobs import get_or_create_job_logger, push_log

log = logging.getLogger("tcp_worker")
log.setLevel(logging.DEBUG)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[TCP] %(message)s"))
    log.addHandler(_h)

# ── Connected worker tracking ──────────────────────────────────

_idle_workers: dict[str, list["TCPWorker"]] = {"ps4": [], "ps5": []}
_lock = asyncio.Lock()

HEARTBEAT_INTERVAL = 30  # seconds


class TCPWorker:
    def __init__(self, reader, writer, platform, worker_key):
        self.reader: asyncio.StreamReader = reader
        self.writer: asyncio.StreamWriter = writer
        self.platform: str = platform
        self.worker_key: str = worker_key
        self.addr = writer.get_extra_info("peername")

    def __repr__(self):
        return f"<TCPWorker {self.platform} {self.addr}>"


# ── Protocol framing ──────────────────────────────────────────


async def send_msg(writer: asyncio.StreamWriter, data: dict):
    payload = json.dumps(data, separators=(",", ":")).encode()
    writer.write(struct.pack(">I", len(payload)))
    writer.write(payload)
    await writer.drain()


async def recv_msg(reader: asyncio.StreamReader) -> dict:
    length_bytes = await reader.readexactly(4)
    length = struct.unpack(">I", length_bytes)[0]
    if length > 16 * 1024 * 1024:  # 16MB sanity limit for JSON messages
        raise ValueError(f"Message too large: {length}")
    payload = await reader.readexactly(length)
    return json.loads(payload.decode())


async def send_file_data(writer: asyncio.StreamWriter, path: str):
    """Send file contents over TCP (preceded by a file_data message)."""
    size = os.path.getsize(path)
    await send_msg(writer, {"type": "file_data", "size": size})
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            writer.write(chunk)
    await writer.drain()


async def recv_file_data(reader: asyncio.StreamReader, dest_path: str, size: int):
    """Receive exactly 'size' bytes from reader and write to dest_path."""
    remaining = size
    with open(dest_path, "wb") as f:
        while remaining > 0:
            chunk_size = min(remaining, 65536)
            data = await reader.readexactly(chunk_size)
            f.write(data)
            remaining -= len(data)


# ── Worker key validation ──────────────────────────────────────


async def _validate_key(key: str) -> bool:
    if WORKER_KEY and hmac.compare_digest(key, WORKER_KEY):
        return True
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM worker_keys WHERE key = ? AND is_active = 1", (key,)
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await db.close()


async def _update_worker_heartbeat(key: str, platform: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE worker_keys SET last_used = CURRENT_TIMESTAMP, last_platform = ? "
            "WHERE key = ?",
            (platform, key),
        )
        await db.execute(
            "UPDATE worker_keys SET online_since = datetime('now') "
            "WHERE key = ? AND (last_used IS NULL OR last_used < datetime('now', '-300 seconds'))",
            (key,),
        )
        if platform == "ps5":
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('last_ps5_worker', datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value = datetime('now')"
            )
        await db.commit()
    finally:
        await db.close()


# ── Job queries ────────────────────────────────────────────────


async def _get_next_job(platform: str) -> dict | None:
    """Find next queued job for the given platform and atomically claim it.

    NOTE: Temporarily restricted to root user only while TCP protocol is tested.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT j.id, j.user_id, j.operation, j.params FROM jobs j "
            "JOIN users u ON j.user_id = u.id "
            "WHERE j.status = 'queued' AND u.username = 'root' "
            "ORDER BY j.created_at ASC LIMIT 20"
        )
        rows = await cursor.fetchall()
        for row in rows:
            params = json.loads(row["params"]) if row["params"] else {}
            job_platform = params.get("platform", "ps4")
            if job_platform == "unknown":
                job_platform = "ps4"
            if job_platform != platform:
                continue
            # Atomically claim it
            cursor2 = await db.execute(
                "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (row["id"],),
            )
            if cursor2.rowcount == 0:
                continue  # someone else grabbed it
            await db.commit()
            return {
                "id": row["id"],
                "operation": row["operation"],
                "params": params,
            }
        return None
    finally:
        await db.close()


async def _build_job_zip(job_id: str) -> str | None:
    """Create a zip of the job's upload files. Returns zip path or None."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT params FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        log.error("_build_job_zip: no row for job %s", job_id)
        return None

    params = json.loads(row["params"]) if row["params"] else {}
    upload_dir = params.get("upload_dir") or params.get("saves_dir")
    if params.get("saves_dir"):
        upload_dir = os.path.dirname(params["saves_dir"])

    log.warning(
        "_build_job_zip: job=%s upload_dir=%s isdir=%s",
        job_id,
        upload_dir,
        os.path.isdir(upload_dir) if upload_dir else "N/A",
    )

    if not upload_dir or not os.path.isdir(upload_dir):
        return None

    tmp_path = os.path.join(os.path.dirname(upload_dir), f"{job_id}_tcp_worker.zip")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        for root, _, files in os.walk(upload_dir):
            for f in files:
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, upload_dir)
                zf.write(filepath, arcname)
    return tmp_path


def _validate_result_zip(path: str) -> str | None:
    """Validate a result zip. Returns error string or None if valid."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            entries = zf.infolist()
            if not entries:
                return "Result zip is empty"
            for entry in entries:
                if entry.file_size > 0 and entry.compress_size == 0:
                    return f"Result zip corrupt: {entry.filename}"
            with zf.open(entries[0]) as f:
                f.read(1)
    except zipfile.BadZipFile as e:
        return f"Result zip invalid: {e}"
    except OSError as e:
        return f"Result zip unreadable: {e}"
    return None


# ── Status/log handlers (reuse existing SSE + DB logic) ────────


async def _handle_log(msg: dict):
    job_id = msg.get("job_id", "")
    level = msg.get("level", "INFO")
    text = msg.get("msg", "")
    push_log(job_id, level, text)

    entry = json.dumps({"level": level, "msg": text})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET logs = CASE WHEN logs IS NULL THEN ? "
            "ELSE logs || '\n' || ? END WHERE id = ?",
            (entry, entry, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _handle_status(msg: dict, worker_key: str):
    """Process a status update from the worker."""
    job_id = msg.get("job_id", "")
    status = msg.get("status", "")
    error = msg.get("error")

    if status not in ("running", "done", "failed"):
        return

    db = await get_db()
    try:
        fields = ["status = ?"]
        values = [status]
        if error:
            fields.append("error = ?")
            values.append(error)
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
        if status == "done" and worker_key:
            await db.execute(
                "UPDATE worker_keys SET jobs_completed = jobs_completed + 1 "
                "WHERE key = ?",
                (worker_key,),
            )
        await db.commit()
    finally:
        await db.close()

    # Broadcast via SSE
    job = get_or_create_job_logger(job_id)
    if job:
        job.status = status
        if error:
            job.error = error
        job.logger._broadcast({"level": "STATUS", "msg": status})

    # Post-completion: extract title, capture sample
    if status == "done":
        try:
            from routes.api import _extract_title_from_zip
            from services.samples import maybe_store_sample_from_zip
            from services.titles import lookup_title

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

                    tid = jp.get("title_id", "")
                    if tid and jrow["operation"] == "decrypt" and os.path.exists(rp):
                        platform = jp.get("platform", "ps4")
                        await maybe_store_sample_from_zip(tid, rp, platform)
            finally:
                await db2.close()
        except Exception:
            pass


async def _handle_file_request(worker: TCPWorker, msg: dict):
    """Worker is requesting job files — build zip and stream it."""
    job_id = msg.get("job_id", "")
    zip_path = await _build_job_zip(job_id)
    if not zip_path:
        await send_msg(worker.writer, {"type": "file_data", "size": 0})
        return
    try:
        await send_file_data(worker.writer, zip_path)
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass


async def _handle_result_upload(worker: TCPWorker, msg: dict):
    """Receive result file from worker."""
    job_id = msg.get("job_id", "")
    size = msg.get("size", 0)

    result_dir = os.path.join("workspace", "results")
    os.makedirs(result_dir, exist_ok=True)
    result_path = os.path.join(result_dir, f"{job_id}.zip")

    await recv_file_data(worker.reader, result_path, size)

    # Validate
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
        await send_msg(worker.writer, {"type": "result_ack", "ok": False, "error": err})
        return

    # Update DB
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET result_path = ? WHERE id = ?",
            (result_path, job_id),
        )
        await db.commit()
    finally:
        await db.close()

    job = get_or_create_job_logger(job_id)
    if job:
        job.result_path = result_path

    await send_msg(worker.writer, {"type": "result_ack", "ok": True})


# ── Main client handler ───────────────────────────────────────


async def handle_worker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    log.info("TCP worker connected from %s", addr)

    try:
        # Auth
        msg = await asyncio.wait_for(recv_msg(reader), timeout=10)
        if msg.get("type") != "auth":
            await send_msg(writer, {"type": "auth_fail", "reason": "expected auth"})
            return

        key = msg.get("key", "")
        platform = msg.get("platform", "ps4")

        if not await _validate_key(key):
            await send_msg(writer, {"type": "auth_fail", "reason": "invalid key"})
            log.warning("TCP worker auth failed from %s", addr)
            return

        await send_msg(writer, {"type": "auth_ok"})
        await _update_worker_heartbeat(key, platform)

        worker = TCPWorker(reader, writer, platform, key)
        log.info("TCP worker authenticated: %s (%s)", addr, platform)

        # Main message loop
        while True:
            msg = await asyncio.wait_for(recv_msg(reader), timeout=120)
            msg_type = msg.get("type", "")

            if msg_type == "ready":
                # Try to find a job for this worker
                job = await _get_next_job(platform)
                if job:
                    log.info("Dispatching job %s to %s", job["id"], worker)
                    await send_msg(
                        writer,
                        {
                            "type": "job",
                            "id": job["id"],
                            "operation": job["operation"],
                            "params": job["params"],
                        },
                    )
                    # Update heartbeat
                    await _update_worker_heartbeat(key, platform)
                else:
                    # No job — add to idle pool and wait
                    async with _lock:
                        if worker not in _idle_workers[platform]:
                            _idle_workers[platform].append(worker)
                    await send_msg(writer, {"type": "no_job"})
                    await _update_worker_heartbeat(key, platform)

            elif msg_type == "log":
                await _handle_log(msg)

            elif msg_type == "status":
                await _handle_status(msg, key)

            elif msg_type == "file_request":
                await _handle_file_request(worker, msg)

            elif msg_type == "result_start":
                await _handle_result_upload(worker, msg)

            elif msg_type == "heartbeat":
                await _update_worker_heartbeat(key, platform)
                await send_msg(writer, {"type": "heartbeat_ack"})

            else:
                log.warning("Unknown message type from %s: %s", addr, msg_type)

    except asyncio.TimeoutError:
        log.info("TCP worker %s timed out", addr)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        log.info("TCP worker %s disconnected", addr)
    except Exception:
        log.exception("TCP worker %s error", addr)
    finally:
        async with _lock:
            for platform_list in _idle_workers.values():
                try:
                    # Remove any reference to this writer
                    platform_list[:] = [
                        w for w in platform_list if w.writer is not writer
                    ]
                except ValueError:
                    pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ── Proactive job dispatch ─────────────────────────────────────


async def notify_job_available(platform: str):
    """Called when a new job is queued. Dispatches to an idle TCP worker if one exists."""
    async with _lock:
        workers = _idle_workers.get(platform, [])
        if not workers:
            return
        worker = workers.pop(0)

    try:
        job = await _get_next_job(platform)
        if job:
            log.info("Proactive dispatch: job %s to %s", job["id"], worker)
            await send_msg(
                worker.writer,
                {
                    "type": "job",
                    "id": job["id"],
                    "operation": job["operation"],
                    "params": job["params"],
                },
            )
        else:
            # Job was grabbed by someone else, put worker back
            async with _lock:
                _idle_workers[platform].append(worker)
    except Exception:
        log.exception("Failed to dispatch to %s", worker)


# ── Server startup ─────────────────────────────────────────────


async def start_tcp_server(port: int = 42069):
    server = await asyncio.start_server(
        handle_worker,
        "0.0.0.0",
        port,
        reuse_address=True,
        reuse_port=True,
        start_serving=True,
    )
    log.info("TCP worker server listening on port %d", port)
    return server
