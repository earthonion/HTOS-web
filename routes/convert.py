import os
import shutil
from quart import Blueprint, render_template, request, session, redirect, url_for, flash

from auth import login_required
from services.jobs import create_job, start_job
from services.files import save_uploaded_files, create_result_zip, cleanup_upload, DangerousFileError
from utils.namespaces import Converter, Crypto
from utils.workspace import init_workspace, cleanup_simple
from utils.extras import completed_print
from data.converter.exceptions import ConverterError
from data.crypto.exceptions import CryptoError

convert_bp = Blueprint("convert", __name__)

GAME_CHOICES = ["GTA V", "RDR 2", "BL 3", "TTWL", "XENO 2"]

@convert_bp.route("/convert", methods=["GET", "POST"])
@login_required
async def convert():
    if request.method == "POST":
        form = await request.form
        game = form.get("game")
        platform_choice = form.get("platform", "")
        files = (await request.files).getlist("files")

        if not game or game not in GAME_CHOICES:
            await flash("Please select a game.", "error")
            return await render_template("convert.html", games=GAME_CHOICES)

        if not files or not files[0].filename:
            await flash("Please upload save files.", "error")
            return await render_template("convert.html", games=GAME_CHOICES)

        user_id = session["user_id"]
        job = await create_job(user_id, "convert")
        try:
            upload_dir = await save_uploaded_files(files, user_id, job.job_id)
        except DangerousFileError as e:
            await flash(str(e), "error")
            return await render_template("convert.html", games=GAME_CHOICES)

        start_job(job, _run_convert(job, upload_dir, game, platform_choice))
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("convert.html", games=GAME_CHOICES)


async def _run_convert(job, upload_dir: str, game: str, platform_choice: str):
    from app_core.helpers import prepare_files_input_folder
    from aiofiles.os import makedirs

    logger = job.logger
    logger.info(f"Starting conversion ({game})...")

    newUPLOAD_ENCRYPTED, newUPLOAD_DECRYPTED, newDOWNLOAD_ENCRYPTED, newPNG_PATH, newPARAM_PATH, newDOWNLOAD_DECRYPTED, newKEYSTONE_PATH = init_workspace()
    workspace_folders = [newUPLOAD_ENCRYPTED, newUPLOAD_DECRYPTED, newDOWNLOAD_ENCRYPTED,
                        newPNG_PATH, newPARAM_PATH, newDOWNLOAD_DECRYPTED, newKEYSTONE_PATH]
    for folder in workspace_folders:
        await makedirs(folder, exist_ok=True)

    output_dir = os.path.join("workspace", "results", job.job_id)
    os.makedirs(output_dir, exist_ok=True)

    try:
        files = await prepare_files_input_folder(job.settings, upload_dir, newUPLOAD_DECRYPTED)
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
            logger.info(f"Converting {basename}, {info}.")

            result = None
            try:
                match game:
                    case "GTA V":
                        result = await Converter.Rstar.convert_file_GTAV(savegame)
                    case "RDR 2":
                        result = await Converter.Rstar.convert_file_RDR2(savegame)
                    case "BL 3":
                        result = await Converter.BL3.convert_file(None, None, savegame, False, None)
                        if not result:
                            if platform_choice == "pc":
                                result = await _bl3_pc_to_ps4(savegame, False)
                            else:
                                result = await _bl3_ps4_to_pc(savegame, False)
                    case "TTWL":
                        result = await Converter.BL3.convert_file(None, None, savegame, True, None)
                        if not result:
                            if platform_choice == "pc":
                                result = await _bl3_pc_to_ps4(savegame, True)
                            else:
                                result = await _bl3_ps4_to_pc(savegame, True)
                    case "XENO 2":
                        result = await Converter.Xeno2.convert_file(savegame)

            except ConverterError as e:
                await cleanup_simple(workspace_folders)
                logger.error(f"{str(e)} Stopping...")
                await job.set_status("failed", error=str(e))
                return
            except Exception:
                await cleanup_simple(workspace_folders)
                logger.exception("Unexpected error. Stopping...")
                await job.set_status("failed", error="Unexpected error")
                return

            if result is None or result == "ERROR":
                await cleanup_simple(workspace_folders)
                logger.error("Invalid save. Stopping...")
                await job.set_status("failed", error="Invalid save")
                return

            completed.append(basename)
            logger.info(f"Converted {basename} ({result}), {info}.")
            j += 1

        out = os.path.join(output_dir, rand_str)
        shutil.copytree(out_path, out, dirs_exist_ok=True)
        finished_files = completed_print(completed)
        logger.info(f"Converted {finished_files} (batch {i}/{batches}).")
        i += 1

    await cleanup_simple(workspace_folders)

    zip_path = create_result_zip(output_dir, job.job_id)
    await job.set_status("done", result_path=zip_path)
    logger.info("Done! Your files are ready for download.")
    cleanup_upload(job.user_id, job.job_id)


async def _bl3_ps4_to_pc(filepath: str, ttwl: bool) -> str:
    try:
        await Crypto.BL3.encrypt_file(filepath, "pc", ttwl)
    except CryptoError as e:
        raise ConverterError(str(e))
    except (ValueError, IOError, IndexError):
        raise ConverterError("Invalid save!")
    return Converter.BL3.obtain_ret_val("ps4")


async def _bl3_pc_to_ps4(filepath: str, ttwl: bool) -> str:
    try:
        await Crypto.BL3.encrypt_file(filepath, "ps4", ttwl)
    except CryptoError as e:
        raise ConverterError(str(e))
    except (ValueError, IOError, IndexError):
        raise ConverterError("Invalid save!")
    return Converter.BL3.obtain_ret_val("pc")
