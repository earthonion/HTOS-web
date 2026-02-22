from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from models import get_db
from services.jobs import create_job
from services.files import save_uploaded_files
from utils.constants import SAVEBLOCKS_MIN, SAVEBLOCKS_MAX
from utils.orbis import validate_savedirname
from utils.conversions import mb_to_saveblocks

createsave_bp = Blueprint("createsave", __name__)

@createsave_bp.route("/createsave", methods=["GET", "POST"])
@login_required
async def createsave():
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
        files = (await request.files).getlist("files")
        savename = form.get("savename", "").strip()
        saveblocks_str = form.get("saveblocks", "").strip()
        savesize_mb_str = form.get("savesize_mb", "").strip()
        ignore_secondlayer = form.get("ignore_secondlayer") == "on"

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("createsave.html", profiles=profiles)

        if not files or not files[0].filename:
            await flash("Please upload files.", "error")
            return await render_template("createsave.html", profiles=profiles)

        if not validate_savedirname(savename):
            await flash("Invalid save name.", "error")
            return await render_template("createsave.html", profiles=profiles)

        # Parse saveblocks
        saveblocks = None
        if saveblocks_str:
            try:
                saveblocks = int(saveblocks_str, 16) if saveblocks_str.lower().startswith("0x") else int(saveblocks_str)
            except ValueError:
                pass
        if saveblocks is None and savesize_mb_str:
            try:
                mb = int(savesize_mb_str, 16) if savesize_mb_str.lower().startswith("0x") else int(savesize_mb_str)
                saveblocks = mb_to_saveblocks(mb)
            except ValueError:
                pass

        if saveblocks is None or not (SAVEBLOCKS_MIN <= saveblocks <= SAVEBLOCKS_MAX):
            await flash("Invalid save size.", "error")
            return await render_template("createsave.html", profiles=profiles)

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
            return await render_template("createsave.html", profiles=profiles)

        account_id = profile["account_id"]
        job = await create_job(user_id, "createsave", {
            "account_id": account_id,
            "savename": savename,
            "saveblocks": saveblocks,
            "ignore_secondlayer": ignore_secondlayer,
        })
        upload_dir = await save_uploaded_files(files, user_id, job.job_id)
        await job.update_params({"upload_dir": upload_dir})

        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("createsave.html", profiles=profiles)
