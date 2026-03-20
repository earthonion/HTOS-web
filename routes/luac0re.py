import os
import shutil
import uuid

from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from models import get_db
from services.jobs import create_job
from services.files import account_id_to_usb

luac0re_bp = Blueprint("luac0re", __name__)

PREBUILT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prebuilt", "luac0re")

VERSIONS = {
    "CUSA03474": {"title": "Star Wars: Racer Revenge (US)", "save": "SLUS-20268"},
    "CUSA03492": {"title": "Star Wars: Racer Revenge (EU)", "save": "SLES-50366"},
}


@luac0re_bp.route("/luac0re", methods=["GET", "POST"])
@login_required
async def luac0re():
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
        version = form.get("version")

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("luac0re.html", profiles=profiles, versions=VERSIONS)

        if version not in VERSIONS:
            await flash("Please select a version.", "error")
            return await render_template("luac0re.html", profiles=profiles, versions=VERSIONS)

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
            return await render_template("luac0re.html", profiles=profiles, versions=VERSIONS)

        account_id = profile["account_id"]
        info = VERSIONS[version]

        # Copy prebuilt save files to workspace
        temp_job_id = str(uuid.uuid4())
        upload_dir = os.path.join("workspace", "uploads", str(user_id), temp_job_id)
        os.makedirs(upload_dir, exist_ok=True)

        src_dir = os.path.join(PREBUILT_DIR, version)
        save_name = info["save"]
        shutil.copy2(os.path.join(src_dir, save_name), os.path.join(upload_dir, save_name))
        shutil.copy2(os.path.join(src_dir, f"{save_name}.bin"), os.path.join(upload_dir, f"{save_name}.bin"))

        job = await create_job(user_id, "resign", {
            "account_id": account_id,
            "upload_dir": upload_dir,
            "platform": "ps4",
        }, ready=True)

        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("luac0re.html", profiles=profiles, versions=VERSIONS)
