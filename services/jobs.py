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
        self._broadcast({"level": "INFO", "msg": msg, "time": datetime.now().isoformat()})

    def warning(self, msg: str):
        self._broadcast({"level": "WARNING", "msg": msg, "time": datetime.now().isoformat()})

    def error(self, msg: str):
        self._broadcast({"level": "ERROR", "msg": msg, "time": datetime.now().isoformat()})

    def exception(self, msg: str):
        tb = traceback.format_exc()
        full = f"{tb}\n{msg}"
        self._broadcast({"level": "EXCEPTION", "msg": full, "time": datetime.now().isoformat()})

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
    def __init__(self, job_id: str, user_id: int, operation: str, params: dict | None = None):
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
                (json.dumps(self.params), self.job_id)
            )
            await db.commit()
        finally:
            await db.close()

    async def set_status(self, status: str, result_path: str | None = None, error: str | None = None):
        self.status = status
        self.result_path = result_path or self.result_path
        self.error = error or self.error
        db = await get_db()
        try:
            await db.execute(
                "UPDATE jobs SET status = ?, result_path = ?, error = ? WHERE id = ?",
                (status, self.result_path, self.error, self.job_id)
            )
            await db.commit()
        finally:
            await db.close()


async def create_job(user_id: int, operation: str, params: dict | None = None, ready: bool = True) -> Job:
    job_id = str(uuid.uuid4())
    params_json = json.dumps(params) if params else None
    status = "queued" if ready else "pending"
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, user_id, operation, status, params) VALUES (?, ?, ?, ?, ?)",
            (job_id, user_id, operation, status, params_json)
        )
        await db.commit()
    finally:
        await db.close()

    job = Job(job_id, user_id, operation, params)
    job.status = status
    _jobs[job_id] = job
    return job


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
            (user_id,)
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
