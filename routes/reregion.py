import json
import os
import shutil

from quart import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import login_required
from config import CHUNK_DIR
from models import get_db
from services.files import detect_platform_in_dir
from services.jobs import create_job
from services.workers import ps5_workers_online

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
        saves_upload_ids_json = form.get("saves_upload_ids")
        sample_upload_ids_json = form.get("sample_upload_ids")

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("reregion.html", profiles=profiles)

        if not saves_upload_ids_json and (not saves or not saves[0].filename):
            await flash("Please upload save files to re-region.", "error")
            return await render_template("reregion.html", profiles=profiles)

        if not sample_upload_ids_json and (
            not sample_files or not sample_files[0].filename
        ):
            await flash(
                "Please upload a sample save pair from the target region.", "error"
            )
            return await render_template("reregion.html", profiles=profiles)

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT account_id FROM profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            )
            profile = await cursor.fetchone()
        finally:
            await db.close()

        if not profile:
            await flash("Invalid profile.", "error")
            return await render_template("reregion.html", profiles=profiles)

        account_id = profile["account_id"]
        import uuid as _uuid

        temp_job_id = str(_uuid.uuid4())

        # Save both sets of files
        upload_dir = os.path.join("workspace", "uploads", str(user_id), temp_job_id)
        saves_dir = os.path.join(upload_dir, "saves")
        sample_dir = os.path.join(upload_dir, "sample")
        os.makedirs(saves_dir, exist_ok=True)
        os.makedirs(sample_dir, exist_ok=True)

        if saves_upload_ids_json:
            for uid in json.loads(saves_upload_ids_json):
                chunk_dir = os.path.join(CHUNK_DIR, uid)
                meta_path = os.path.join(chunk_dir, "meta.json")
                if os.path.isfile(meta_path):
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                    src = os.path.join(chunk_dir, meta["filename"])
                    if os.path.isfile(src):
                        shutil.move(src, os.path.join(saves_dir, meta["filename"]))
                shutil.rmtree(chunk_dir, ignore_errors=True)
        else:
            for f in saves:
                if not f.filename or f.filename.endswith("/"):
                    continue
                dest = os.path.join(saves_dir, os.path.basename(f.filename))
                await f.save(dest)

        if sample_upload_ids_json:
            for uid in json.loads(sample_upload_ids_json):
                chunk_dir = os.path.join(CHUNK_DIR, uid)
                meta_path = os.path.join(chunk_dir, "meta.json")
                if os.path.isfile(meta_path):
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                    src = os.path.join(chunk_dir, meta["filename"])
                    if os.path.isfile(src):
                        shutil.move(src, os.path.join(sample_dir, meta["filename"]))
                shutil.rmtree(chunk_dir, ignore_errors=True)
        else:
            for f in sample_files:
                if not f.filename or f.filename.endswith("/"):
                    continue
                dest = os.path.join(sample_dir, os.path.basename(f.filename))
                await f.save(dest)

        platform = detect_platform_in_dir(saves_dir)

        if platform == "ps5":
            if not await ps5_workers_online():
                await flash("PS5 saves not currently supported!", "error")
                return await render_template("reregion.html", profiles=profiles)

        job = await create_job(
            user_id,
            "reregion",
            {
                "account_id": account_id,
                "saves_dir": saves_dir,
                "sample_dir": sample_dir,
                "platform": platform,
            },
            ready=True,
        )
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("reregion.html", profiles=profiles)
