import os
import zipfile
from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from models import get_db
from services.jobs import create_job
from utils.constants import (
    SAVEBLOCKS_MIN, SAVEBLOCKS_MAX,
    SCE_SYS_NAME, PARAM_NAME,
)
from utils.orbis import validate_savedirname, sfo_ctx_create
from services.files import _read_account_id_from_sfo, FileTooLargeError, DangerousFileError, check_dangerous_files, check_zip_safety, _check_file_sizes, _strip_sdimg_prefix, resolve_chunked_uploads, account_id_to_usb
from services.workers import ps5_workers_online

encrypt_bp = Blueprint("encrypt", __name__)


@encrypt_bp.route("/encrypt", methods=["GET", "POST"])
@login_required
async def encrypt():
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
        files_dict = await request.files
        zipfile_upload = files_dict.get("zipfile")
        folder_files = files_dict.getlist("folder_files")
        upload_ids_json = form.get("upload_ids")
        folder_upload_ids_json = form.get("folder_upload_ids")

        # Detect folder upload
        is_folder_upload = bool(folder_files and folder_files[0].filename)

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        if not upload_ids_json and not folder_upload_ids_json and not is_folder_upload and (not zipfile_upload or not zipfile_upload.filename):
            await flash("Please upload a zip file or folder.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        if not upload_ids_json and not folder_upload_ids_json and not is_folder_upload and not zipfile_upload.filename.endswith(".zip"):
            await flash("File must be a .zip file.", "error")
            return await render_template("encrypt.html", profiles=profiles)

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
            return await render_template("encrypt.html", profiles=profiles)

        account_id = account_id_to_usb(profile["account_id"])
        import uuid as _uuid
        temp_job_id = str(_uuid.uuid4())

        # Save the zip to workspace
        upload_dir = os.path.join("workspace", "uploads", str(user_id), temp_job_id)
        os.makedirs(upload_dir, exist_ok=True)

        extract_dir = os.path.join(upload_dir, "extracted")

        if is_folder_upload:
            # Folder upload: save files preserving relative paths
            for f in folder_files:
                if not f.filename:
                    continue
                dest = os.path.join(extract_dir, f.filename)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                await f.save(dest)
        elif upload_ids_json:
            import json as _json
            upload_ids = _json.loads(upload_ids_json)
            chunked_dir = await resolve_chunked_uploads(upload_ids, user_id, temp_job_id)
            zip_path = None
            for f in os.listdir(chunked_dir):
                if f.endswith(".zip"):
                    zip_path = os.path.join(chunked_dir, f)
                    break
            if not zip_path:
                await flash("No .zip file found in upload.", "error")
                return await render_template("encrypt.html", profiles=profiles)
            try:
                check_zip_safety(zip_path)
            except DangerousFileError as e:
                await flash(str(e), "error")
                return await render_template("encrypt.html", profiles=profiles)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        else:
            zip_path = os.path.join(upload_dir, zipfile_upload.filename)
            await zipfile_upload.save(zip_path)
            try:
                check_zip_safety(zip_path)
            except DangerousFileError as e:
                await flash(str(e), "error")
                return await render_template("encrypt.html", profiles=profiles)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

        try:
            check_dangerous_files(extract_dir)
        except DangerousFileError as e:
            await flash(str(e), "error")
            return await render_template("encrypt.html", profiles=profiles)

        # Strip PS5 sdimg_ prefix and check file sizes
        _strip_sdimg_prefix(extract_dir)
        for sub in os.listdir(extract_dir):
            subpath = os.path.join(extract_dir, sub)
            if os.path.isdir(subpath):
                _strip_sdimg_prefix(subpath)
        try:
            _check_file_sizes(extract_dir)
            for sub in os.listdir(extract_dir):
                subpath = os.path.join(extract_dir, sub)
                if os.path.isdir(subpath):
                    _check_file_sizes(subpath)
        except FileTooLargeError as e:
            await flash(f"Save file too large: {e}. Worker cannot process files this big.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        # Find the save folder containing sce_sys
        save_dir = None
        for root, dirs, _files in os.walk(extract_dir):
            if SCE_SYS_NAME in dirs:
                save_dir = root
                break

        if save_dir is None:
            await flash("No sce_sys folder found. Make sure your save folder contains sce_sys.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        savename = os.path.basename(save_dir)
        # For folder uploads, use the top-level folder name
        upload_filename = ""
        if is_folder_upload and folder_files and folder_files[0].filename:
            upload_filename = folder_files[0].filename.split("/")[0]
        elif zipfile_upload and zipfile_upload.filename:
            upload_filename = zipfile_upload.filename
        if save_dir == extract_dir and upload_filename:
            savename = os.path.splitext(upload_filename)[0]
        # Strip decrypt prefix (dec_NAME_CUSAXXXXX -> NAME)
        if savename.startswith("dec_"):
            savename = savename[4:]
        import re
        savename = re.sub(r'_CUSA\d{5}$', '', savename)
        if not savename or savename == "extracted":
            savename = os.path.splitext(upload_filename)[0] if upload_filename else "save"
            if "-" in savename:
                parts = savename.split("-", 1)
                savename = parts[1] if len(parts) > 1 else savename
            if "_20" in savename:
                savename = savename[:savename.rfind("_20")]

        if not validate_savedirname(savename):
            await flash(f"Invalid save name derived from zip: {savename}", "error")
            return await render_template("encrypt.html", profiles=profiles)

        # Read saveblocks from param.sfo
        sfo_path = os.path.join(save_dir, SCE_SYS_NAME, PARAM_NAME)
        if not os.path.isfile(sfo_path):
            await flash("No sce_sys/param.sfo found in zip.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        sfo_ctx = await sfo_ctx_create(sfo_path)
        saveblocks = None
        for param in sfo_ctx.params:
            if param.key == "SAVEDATA_BLOCKS":
                from utils.type_helpers import uint64
                blocks = uint64(param.value, "little")
                saveblocks = blocks.value
                break
        if saveblocks is None:
            await flash("Could not read SAVEDATA_BLOCKS from param.sfo.", "error")
            return await render_template("encrypt.html", profiles=profiles)

        if not (SAVEBLOCKS_MIN <= saveblocks <= SAVEBLOCKS_MAX):
            await flash(f"Invalid save blocks: {saveblocks}", "error")
            return await render_template("encrypt.html", profiles=profiles)

        platform = "ps5" if form.get("platform") == "ps5" else "ps4"

        # Read account ID from param.sfo (PS4: 0x15C, PS5: 0x1B8)
        sfo_account_id = _read_account_id_from_sfo(sfo_path, platform)
        if platform == "ps5" and not await ps5_workers_online():
            await flash("PS5 saves not currently supported!", "error")
            return await render_template("encrypt.html", profiles=profiles)
        params = {
            "account_id": account_id,
            "savename": savename,
            "saveblocks": saveblocks,
            "upload_dir": save_dir,
            "platform": platform,
        }
        if sfo_account_id:
            params["sfo_account_id"] = sfo_account_id
        job = await create_job(user_id, "encrypt", params, ready=True)

        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("encrypt.html", profiles=profiles)
