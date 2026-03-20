from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from services.jobs import create_job
from services.files import save_uploaded_files, extract_account_id, detect_platform_in_dir, FileTooLargeError, InvalidSaveFilesError, DangerousFileError, validate_save_pairs, resolve_chunked_uploads
from services.workers import ps5_workers_online

decrypt_bp = Blueprint("decrypt", __name__)

@decrypt_bp.route("/decrypt", methods=["GET", "POST"])
@login_required
async def decrypt():
    if request.method == "POST":
        form = await request.form
        files = (await request.files).getlist("saves")
        include_sce_sys = form.get("include_sce_sys") == "on"
        ignore_secondlayer = form.get("ignore_secondlayer") == "on"

        upload_ids_json = form.get("upload_ids")

        if not upload_ids_json and (not files or not files[0].filename):
            await flash("Please upload save files.", "error")
            return await render_template("decrypt.html")

        user_id = session["user_id"]
        job = await create_job(user_id, "decrypt", {
            "include_sce_sys": include_sce_sys,
            "ignore_secondlayer": ignore_secondlayer,
        }, ready=False)
        try:
            if upload_ids_json:
                import json
                upload_ids = json.loads(upload_ids_json)
                upload_dir = await resolve_chunked_uploads(upload_ids, user_id, job.job_id)
            else:
                upload_dir = await save_uploaded_files(files, user_id, job.job_id)
        except FileTooLargeError as e:
            await flash(f"Save file too large: {e}. Worker cannot process files this big.", "error")
            return await render_template("decrypt.html")
        except DangerousFileError as e:
            await flash(str(e), "error")
            return await render_template("decrypt.html")
        platform = detect_platform_in_dir(upload_dir)
        if platform != "ps5":
            try:
                validate_save_pairs(upload_dir)
            except InvalidSaveFilesError as e:
                await flash(str(e), "error")
                return await render_template("decrypt.html")
        params = {"upload_dir": upload_dir, "platform": platform}
        acct = extract_account_id(upload_dir, platform)
        if acct:
            params["sfo_account_id"] = acct
        await job.update_params(params)

        if platform == "ps5":
            if not await ps5_workers_online():
                await flash("PS5 saves not currently supported!", "error")
                return await render_template("decrypt.html")
            job.logger.info("Decrypting PS5 save...")
        else:
            job.logger.info("Decrypting PS4 save...")

        await job.set_status("queued")
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("decrypt.html")
