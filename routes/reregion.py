import os
from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from models import get_db
from services.jobs import create_job

reregion_bp = Blueprint("reregion", __name__)

@reregion_bp.route("/reregion", methods=["GET", "POST"])
@login_required
async def reregion():
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
        saves = (await request.files).getlist("saves")
        sample_files = (await request.files).getlist("sample")

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("reregion.html", profiles=profiles)

        if not saves or not saves[0].filename:
            await flash("Please upload save files to re-region.", "error")
            return await render_template("reregion.html", profiles=profiles)

        if not sample_files or not sample_files[0].filename:
            await flash("Please upload a sample save pair from the target region.", "error")
            return await render_template("reregion.html", profiles=profiles)

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
            return await render_template("reregion.html", profiles=profiles)

        account_id = profile["account_id"]
        job = await create_job(user_id, "reregion", {"account_id": account_id})

        # Save both sets of files
        upload_dir = os.path.join("workspace", "uploads", str(user_id), job.job_id)
        saves_dir = os.path.join(upload_dir, "saves")
        sample_dir = os.path.join(upload_dir, "sample")
        os.makedirs(saves_dir, exist_ok=True)
        os.makedirs(sample_dir, exist_ok=True)

        for f in saves:
            await f.save(os.path.join(saves_dir, f.filename))
        for f in sample_files:
            await f.save(os.path.join(sample_dir, f.filename))

        await job.update_params({
            "saves_dir": saves_dir,
            "sample_dir": sample_dir,
        })

        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("reregion.html", profiles=profiles)
