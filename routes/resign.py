from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from models import get_db
from services.jobs import create_job
from services.files import save_uploaded_files, detect_platform_in_dir, resolve_chunked_uploads
from services.workers import ps5_workers_online

resign_bp = Blueprint("resign", __name__)

@resign_bp.route("/resign", methods=["GET", "POST"])
@login_required
async def resign():
    user_id = session["user_id"]
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, account_id FROM profiles WHERE user_id = ?", (user_id,)
        )
        profiles = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    if request.method == "POST":
        form = await request.form
        profile_id = form.get("profile_id")
        files = (await request.files).getlist("saves")

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("resign.html", profiles=profiles)

        upload_ids_json = form.get("upload_ids")

        if not upload_ids_json and (not files or not files[0].filename):
            await flash("Please upload save files.", "error")
            return await render_template("resign.html", profiles=profiles)

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT account_id FROM profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id)
            )
            profile = await cursor.fetchone()
        finally:
            await db.close()

        if not profile:
            await flash("Invalid profile.", "error")
            return await render_template("resign.html", profiles=profiles)

        account_id = profile["account_id"]
        job = await create_job(user_id, "resign", {"account_id": account_id}, ready=False)
        if upload_ids_json:
            import json
            upload_ids = json.loads(upload_ids_json)
            upload_dir = await resolve_chunked_uploads(upload_ids, user_id, job.job_id)
        else:
            upload_dir = await save_uploaded_files(files, user_id, job.job_id)
        platform = detect_platform_in_dir(upload_dir)
        await job.update_params({"upload_dir": upload_dir, "platform": platform})

        if platform == "ps5":
            if not await ps5_workers_online():
                await flash("PS5 saves not currently supported!", "error")
                return await render_template("resign.html", profiles=profiles)
            job.logger.info("Resigning PS5 save...")
        else:
            job.logger.info("Resigning PS4 save...")

        await job.set_status("queued")
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("resign.html", profiles=profiles)
