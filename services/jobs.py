import asyncio
import json
import traceback
import uuid
from datetime import datetime

from models import get_db

# Global registry of active jobs (in-memory for SSE broadcasting)
_jobs: dict[str, "Job"] = {}


class WebLogger:
    """Stores log messages for a job, broadcasts via SSE to subscribers."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.messages: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []

    def _broadcast(self, entry: dict):
        self.messages.append(entry)
        for q in self.subscribers:
            q.put_nowait(entry)

    def info(self, msg: str):
        self._broadcast(
            {"level": "INFO", "msg": msg, "time": datetime.now().isoformat()}
        )

    def warning(self, msg: str):
        self._broadcast(
            {"level": "WARNING", "msg": msg, "time": datetime.now().isoformat()}
        )

    def error(self, msg: str):
        self._broadcast(
            {"level": "ERROR", "msg": msg, "time": datetime.now().isoformat()}
        )

    def exception(self, msg: str):
        tb = traceback.format_exc()
        full = f"{tb}\n{msg}"
        self._broadcast(
            {"level": "EXCEPTION", "msg": full, "time": datetime.now().isoformat()}
        )

    def clear(self):
        self.messages.clear()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    @property
    def value(self):
        """Shim for Settings-like .value access."""
        return False


class ServerSettings:
    """Minimal shim so prepare_save_input_folder(settings, logger, ...) works."""

    class _BoolVal:
        def __init__(self, val=False):
            self.value = val

    recursivity = _BoolVal(False)
    verbose_errors = _BoolVal(True)


class Job:
    def __init__(
        self, job_id: str, user_id: int, operation: str, params: dict | None = None
    ):
        self.job_id = job_id
        self.user_id = user_id
        self.operation = operation
        self.params = params or {}
        self.status = "queued"
        self.logger = WebLogger(job_id)
        self.settings = ServerSettings()
        self.task: asyncio.Task | None = None
        self.result_path: str | None = None
        self.error: str | None = None
        # For encrypt multi-step
        self.event = asyncio.Event()
        self.encrypt_folder: str | None = None
        self.file_list: list[str] | None = None

    async def update_params(self, new_params: dict):
        """Merge new_params into self.params and persist to DB."""
        self.params.update(new_params)
        db = await get_db()
        try:
            await db.execute(
                "UPDATE jobs SET params = ? WHERE id = ?",
                (json.dumps(self.params), self.job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def set_status(
        self, status: str, result_path: str | None = None, error: str | None = None
    ):
        self.status = status
        self.result_path = result_path or self.result_path
        self.error = error or self.error
        db = await get_db()
        try:
            await db.execute(
                "UPDATE jobs SET status = ?, result_path = ?, error = ? WHERE id = ?",
                (status, self.result_path, self.error, self.job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def try_claim(self) -> bool:
        """Atomically transition queued → running. Returns True if we got it,
        False if another worker / fallback beat us. Used by the bridge
        fallback so it doesn't race a worker about to /next-pull the same job."""
        db = await get_db()
        try:
            cursor = await db.execute(
                "UPDATE jobs SET status = 'running' "
                "WHERE id = ? AND status = 'queued'",
                (self.job_id,),
            )
            await db.commit()
            if cursor.rowcount > 0:
                self.status = "running"
                return True
            return False
        finally:
            await db.close()


async def create_job(
    user_id: int, operation: str, params: dict | None = None, ready: bool = True
) -> Job:
    job_id = str(uuid.uuid4())
    params_json = json.dumps(params) if params else None
    status = "queued" if ready else "pending"
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, user_id, operation, status, params) VALUES (?, ?, ?, ?, ?)",
            (job_id, user_id, operation, status, params_json),
        )
        await db.commit()
    finally:
        await db.close()

    job = Job(job_id, user_id, operation, params)
    job.status = status
    _jobs[job_id] = job

    # Notify TCP workers that a job is available
    if ready:
        dispatched = False
        try:
            from services.tcp_worker import _idle_workers, notify_job_available

            platform = (params or {}).get("platform", "ps4")
            if _idle_workers.get(platform):
                asyncio.ensure_future(notify_job_available(platform))
                dispatched = True
        except Exception:
            pass

        # Fallback to bridge if no workers and operation is supported
        if not dispatched and operation in ("decrypt", "encrypt"):
            platform = (params or {}).get("platform", "ps4")
            if platform == "ps4":
                try:
                    from services.worker_bridge import (
                        check_available,
                        is_configured,
                    )

                    if is_configured():
                        asyncio.ensure_future(_try_bridge_fallback(job))
                except Exception:
                    pass

    return job


async def _try_bridge_fallback(job: Job):
    """Attempt to process a job via bridge fallback."""
    from services.worker_bridge import check_available, process_decrypt, process_encrypt

    if not await check_available():
        job.logger.info("No workers or fallback available, waiting for a worker...")
        return

    # Atomic claim — bail silently if a worker has already grabbed this job.
    if not await job.try_claim():
        return

    try:
        job.logger.info("Using fallback processor (queue backlog / high load)...")

        if job.operation == "decrypt":
            result_path = await process_decrypt(job)
        elif job.operation == "encrypt":
            result_path = await process_encrypt(job)
        else:
            # We claimed it but can't handle it — release back to queued.
            await job.set_status("queued")
            return

        await job.set_status("done", result_path=result_path)
        job.logger.info("Done!")
        job.logger._broadcast({"level": "STATUS", "msg": "done"})
    except Exception as e:
        job.logger.error(f"Fallback failed: {e}")
        # Release back to queue so a real worker can retry it.
        await job.set_status("queued", error=None)
        await job.set_status("failed", error=str(e))
        job.logger._broadcast({"level": "STATUS", "msg": "failed"})


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def get_or_create_job_logger(job_id: str) -> Job | None:
    """Get existing job or create a shell Job for SSE broadcasting (used by worker API)."""
    job = _jobs.get(job_id)
    if job is None:
        # Create a minimal in-memory job for SSE broadcasting
        job = Job(job_id, 0, "")
        _jobs[job_id] = job
    return job


async def get_user_jobs(user_id: int) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, operation, status, created_at, error FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


def push_log(job_id: str, level: str, msg: str):
    """Push a log message to a job's SSE subscribers. Called by worker API."""
    job = get_or_create_job_logger(job_id)
    if job:
        entry = {"level": level, "msg": msg, "time": datetime.now().isoformat()}
        job.logger._broadcast(entry)


def start_job(job: Job, coro):
    """Start a background asyncio task for this job."""

    async def _runner():
        try:
            await job.set_status("running")
            await coro
            if job.status == "running":
                await job.set_status("done")
        except Exception as e:
            job.logger.error(str(e))
            await job.set_status("failed", error=str(e))

    job.task = asyncio.create_task(_runner())
    return job


async def bridge_fallback_poller():
    """Background loop: every ~20s, find queued PS4 decrypt/encrypt jobs that
    have been stuck for >60s and hand them to the bridge fallback if it
    has capacity. The fallback's try_claim() ensures no race with workers."""
    import logging as _logging
    import json as _json

    from services.worker_bridge import (
        check_available,
        is_configured,
    )
    from models import get_db as _get_db

    POLL_INTERVAL = 20
    MIN_QUEUED_AGE = 60   # seconds — only consider jobs that have waited
    MAX_BATCH = 3         # how many to hand off per tick (don't flood bridge)
    log = _logging.getLogger("bridge_fallback_poller")

    if not is_configured():
        log.info("bridge not configured; poller disabled")
        return

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            if not await check_available():
                continue
            db = await _get_db()
            try:
                cursor = await db.execute(
                    "SELECT id, user_id, operation, params FROM jobs "
                    "WHERE status = 'queued' "
                    "  AND operation IN ('decrypt', 'encrypt') "
                    "  AND created_at < datetime('now', '-' || ? || ' seconds') "
                    "ORDER BY created_at ASC LIMIT ?",
                    (MIN_QUEUED_AGE, MAX_BATCH),
                )
                rows = [dict(r) for r in await cursor.fetchall()]
            finally:
                await db.close()

            for row in rows:
                # PS4 platform check (fallback only supports PS4)
                params = _json.loads(row["params"]) if row["params"] else {}
                if params.get("platform", "ps4") != "ps4":
                    continue
                job = _jobs.get(row["id"])
                if not job:
                    # Reconstruct from DB so the fallback path has a Job object
                    job = Job(row["id"], row["user_id"], row["operation"], params)
                    _jobs[row["id"]] = job
                log.info(
                    "handing off %s (%s, queued > %ds) to bridge",
                    row["id"][:8], row["operation"], MIN_QUEUED_AGE,
                )
                asyncio.ensure_future(_try_bridge_fallback(job))
        except Exception as e:
            log.warning("poller tick error: %s", e)
