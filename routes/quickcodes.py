import os
import shutil

from quart import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import login_required
from data.cheats.quickcodes import QuickCodes as QC
from data.cheats.quickcodes import QuickCodesError
from services.files import cleanup_upload, create_result_zip, save_uploaded_files
from services.jobs import create_job, start_job
from utils.extras import completed_print
from utils.workspace import cleanup_simple, init_workspace

quickcodes_bp = Blueprint("quickcodes", __name__)


@quickcodes_bp.route("/quickcodes", methods=["GET", "POST"])
@login_required
async def quickcodes():
    if request.method == "POST":
        form = await request.form
        codes = form.get("codes", "").strip()
        files = (await request.files).getlist("files")

        if not codes:
            await flash("Please enter quick codes.", "error")
            return await render_template("quickcodes.html")

        if not files or not files[0].filename:
            await flash("Please upload save files.", "error")
            return await render_template("quickcodes.html")

        try:
            QC("", codes)
        except QuickCodesError as e:
            await flash(f"Invalid codes: {e}", "error")
            return await render_template("quickcodes.html")

        user_id = session["user_id"]
        job = await create_job(user_id, "quickcodes")
        upload_dir = await save_uploaded_files(files, user_id, job.job_id)

        start_job(job, _run_quickcodes(job, upload_dir, codes))
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("quickcodes.html")


async def _run_quickcodes(job, upload_dir: str, codes: str):
    from aiofiles.os import makedirs

    from app_core.helpers import prepare_files_input_folder

    logger = job.logger
    logger.info("Applying codes...")

    qc = QC("", codes)

    (
        newUPLOAD_ENCRYPTED,
        newUPLOAD_DECRYPTED,
        newDOWNLOAD_ENCRYPTED,
        newPNG_PATH,
        newPARAM_PATH,
        newDOWNLOAD_DECRYPTED,
        newKEYSTONE_PATH,
    ) = init_workspace()
    workspace_folders = [
        newUPLOAD_ENCRYPTED,
        newUPLOAD_DECRYPTED,
        newDOWNLOAD_ENCRYPTED,
        newPNG_PATH,
        newPARAM_PATH,
        newDOWNLOAD_DECRYPTED,
        newKEYSTONE_PATH,
    ]
    for folder in workspace_folders:
        await makedirs(folder, exist_ok=True)

    output_dir = os.path.join("workspace", "results", job.job_id)
    os.makedirs(output_dir, exist_ok=True)

    try:
        files = await prepare_files_input_folder(
            job.settings, upload_dir, newUPLOAD_DECRYPTED
        )
    except OSError:
        await cleanup_simple(workspace_folders)
        logger.exception("Unexpected error. Stopping...")
        await job.set_status("failed", error="Unexpected error")
        return

    batches = len(files)
    i = 1
    for entry in files:
        count_entry = len(entry)
        completed = []
        dname = os.path.dirname(entry[0])
        out_path = dname
        rand_str = os.path.basename(dname)

        j = 1
        for savegame in entry:
            info = f"(file {j}/{count_entry}, batch {i}/{batches})"
            basename = os.path.basename(savegame)
            logger.info(f"Applying codes to {basename}, {info}.")

            qc.filePath = savegame
            try:
                await qc.apply_code()
            except QuickCodesError as e:
                await cleanup_simple(workspace_folders)
                logger.error(f"{str(e)} Stopping...")
                await job.set_status("failed", error=str(e))
                return
            except Exception:
                await cleanup_simple(workspace_folders)
                logger.exception("Unexpected error. Stopping...")
                await job.set_status("failed", error="Unexpected error")
                return

            completed.append(basename)
            j += 1

        out = os.path.join(output_dir, rand_str)
        shutil.copytree(out_path, out, dirs_exist_ok=True)
        finished_files = completed_print(completed)
        logger.info(f"Applied codes to {finished_files} (batch {i}/{batches}).")
        i += 1

    await cleanup_simple(workspace_folders)

    zip_path = create_result_zip(output_dir, job.job_id)
    await job.set_status("done", result_path=zip_path)
    logger.info("Done! Your files are ready for download.")
    cleanup_upload(job.user_id, job.job_id)
