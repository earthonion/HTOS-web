import asyncio
import json
import os

from quart import Blueprint, render_template, session, send_file, Response, abort

from auth import login_required
from models import get_db
from services.jobs import get_job, get_or_create_job_logger
from services.files import extract_account_id_from_zip

jobs_bp = Blueprint("jobs", __name__)


async def _load_job_from_db(job_id, user_id):
    """Load job from DB and ensure it's in the in-memory registry for SSE."""
    # Always check DB for latest state (workers may run in separate processes)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, operation, status, result_path, error, params FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id)
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
    return job


@jobs_bp.route("/jobs/<job_id>")
@login_required
async def job_status(job_id):
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)

    # Extract account ID from result zip if not already in params
    if job.status == "done" and job.result_path and not job.params.get("sfo_account_id"):
        if os.path.exists(job.result_path):
            acct = extract_account_id_from_zip(job.result_path)
            if acct:
                job.params["sfo_account_id"] = acct
                await job.update_params({"sfo_account_id": acct})

    return await render_template("job_status.html", job=job)

@jobs_bp.route("/jobs/<job_id>/stream")
@login_required
async def job_stream(job_id):
    job = await _load_job_from_db(job_id, session["user_id"])
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
                    if msg.get("level") == "STATUS" and msg.get("msg") in ("done", "failed"):
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
                    yield f": keepalive\n\n"
        finally:
            job.logger.unsubscribe(q)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

@jobs_bp.route("/jobs/<job_id>/download")
@login_required
async def job_download(job_id):
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)
    if not job.result_path or not os.path.exists(job.result_path):
        abort(404)

    # Build a descriptive filename including save name if available
    save_name = ""
    if job.params:
        save_name = job.params.get("savename", "")
        if not save_name:
            # For decrypt/resign, try to get name from uploaded files
            upload_dir = job.params.get("upload_dir", "")
            if upload_dir and os.path.isdir(upload_dir):
                for f in os.listdir(upload_dir):
                    if not f.endswith(".bin") and not f.endswith(".zip"):
                        save_name = f
                        break

    if save_name:
        filename = f"{job.operation}_{save_name}_{job_id[:8]}.zip"
    else:
        filename = f"{job.operation}_{job_id[:8]}.zip"

    return await send_file(
        job.result_path,
        as_attachment=True,
        attachment_filename=filename
    )

@jobs_bp.route("/jobs/<job_id>/files")
@login_required
async def job_files(job_id):
    """List decrypted files for encrypt phase 2."""
    job = await _load_job_from_db(job_id, session["user_id"])
    if not job:
        abort(404)

    return {"files": job.file_list or [], "status": job.status}
