import asyncio
import io
import json
import os
import re
import shutil
import struct
import time
import uuid
import zipfile
from pathlib import Path

from quart import Blueprint, Response, flash, render_template, request, session

from auth import login_required
from config import UPLOAD_DIR
from models import get_db
from services.files import DangerousFileError, _extract_zips_in_dir, _strip_sdimg_prefix, check_zip_safety
from services.gta6_money import SaveError, edit_bytes, edit_zip, load as load_money_save
from services.jobs import create_job


gta6_bp = Blueprint("gta6", __name__)
WORK_ROOT = Path("workspace") / "gta6"
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_SAVE_FILES = 16


def _cleanup_old():
    now = time.time()
    if not WORK_ROOT.exists():
        return
    for path in WORK_ROOT.iterdir():
        try:
            if path.is_dir() and now - path.stat().st_mtime > 86400:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _editor_model(path):
    saves = []
    for file_index, (name, save) in enumerate(load_money_save(path)):
        inventories = []
        for number, inventory in save.inventories.items():
            inventories.append({
                "number": number,
                "name": inventory.name,
                "bank": inventory.bank,
                "cash": inventory.cash,
                "items_used": inventory.items_used,
                "max_bank": inventory.max_bank(),
                "has_cash": bool(inventory._positions(4163145158)),
            })
        saves.append({"index": file_index, "name": name or "save.jsav", "inventories": inventories})
    if len(saves) > MAX_SAVE_FILES:
        raise SaveError("This package contains too many save files for the basic editor.")
    return saves


def _amount(raw, label):
    value = str(raw or "").replace(",", "").replace("_", "").replace("$", "").strip()
    if not value:
        return None
    try:
        amount = int(value)
    except ValueError as error:
        raise SaveError(f"{label} must be a non-negative whole number.") from error
    if amount < 0 or amount > 0x7FFFFFFFFFFFFFFF:
        raise SaveError(f"{label} is outside the supported range.")
    return amount


def _sfo_values(data):
    if len(data) < 20 or data[:4] != b"\x00PSF":
        raise SaveError("Invalid param.sfo in the decrypted package.")
    key_offset, data_offset, count = struct.unpack_from("<III", data, 8)
    if count > (len(data) - 20) // 16:
        raise SaveError("Invalid param.sfo entry count.")
    values = {}
    for index in range(count):
        base = 20 + index * 16
        key_rel, _fmt, size, _capacity, value_rel = struct.unpack_from("<HHIII", data, base)
        key_pos = key_offset + key_rel
        value_pos = data_offset + value_rel
        if key_pos >= len(data) or value_pos > len(data) or size > len(data) - value_pos:
            continue
        key_end = data.find(b"\x00", key_pos)
        if key_end < 0:
            continue
        key = data[key_pos:key_end].decode("ascii", errors="ignore")
        values[key] = data[value_pos:value_pos + size]
    return values


def _containers(zip_bytes):
    account_id = None
    containers = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = set(archive.namelist())
        for name in sorted(names):
            if name.endswith("chives.json"):
                document = json.loads(archive.read(name))
                directory = document.get("save_directory", "")
                if directory:
                    containers.append({
                        "directory": directory,
                        "title_id": document.get("title_id", ""),
                        "output_name": directory if directory.startswith("sdimg_") else "sdimg_" + directory,
                    })
            elif name.endswith("sce_sys/param.sfo"):
                values = _sfo_values(archive.read(name))
                account_id = account_id or values.get("ACCOUNT_ID", b"").hex()
                if not containers:
                    directory = values.get("SAVEDATA_DIRECTORY", b"").rstrip(b"\x00").decode()
                    if directory:
                        containers.append({
                            "directory": directory,
                            "title_id": values.get("TITLE_ID", b"").rstrip(b"\x00").decode(),
                            "output_name": directory if directory.startswith("sdimg_") else "sdimg_" + directory,
                        })
    if not account_id:
        raise SaveError("The decrypted ZIP has no account ID in sce_sys/param.sfo.")
    unique = {}
    for container in containers:
        unique[container["directory"]] = container
    if not unique:
        raise SaveError("Could not determine the save directory from the decrypted ZIP.")
    return account_id, list(unique.values())


def _patch_account(zip_bytes, account_id):
    raw = bytes.fromhex(account_id)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as destination:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith("sce_sys/param.sfo"):
                if len(data) < 0x1C0:
                    raise SaveError(f"Invalid account metadata in {info.filename}.")
                data = data[:0x1B8] + raw + data[0x1C0:]
            destination.writestr(info, data)
    return output.getvalue()


async def _wait_for_job(job):
    for _ in range(300):
        db = await get_db()
        try:
            cursor = await db.execute("SELECT status, result_path, error FROM jobs WHERE id = ?", (job.job_id,))
            row = await cursor.fetchone()
        finally:
            await db.close()
        if row["status"] == "done":
            if not row["result_path"] or not os.path.isfile(row["result_path"]):
                raise SaveError("The encrypt job completed without a result.")
            return row["result_path"]
        if row["status"] == "failed":
            raise SaveError(row["error"] or "The encrypt job failed.")
        await asyncio.sleep(1)
    raise SaveError("The encrypt job timed out.")


async def _encrypt_containers(user_id, zip_bytes):
    account_id, containers = _containers(zip_bytes)
    zip_bytes = _patch_account(zip_bytes, account_id)
    results = {}
    for container in containers:
        params = {
            "account_id": account_id,
            "savename": container["directory"],
            "platform": "ps5",
            "ignore_secondlayer": False,
        }
        if container["title_id"]:
            params["title_id"] = container["title_id"]
        job = await create_job(user_id, "encrypt", params, ready=False)
        upload_dir = os.path.join(UPLOAD_DIR, str(user_id), job.job_id)
        os.makedirs(upload_dir, exist_ok=True)
        (Path(upload_dir) / "edited.zip").write_bytes(zip_bytes)
        _extract_zips_in_dir(upload_dir)
        _strip_sdimg_prefix(upload_dir)
        await job.update_params({"upload_dir": upload_dir})
        await job.set_status("queued")
        result_path = await _wait_for_job(job)
        with zipfile.ZipFile(result_path) as archive:
            images = [item for item in archive.infolist() if not item.is_dir()]
            if len(images) != 1:
                raise SaveError("The encrypt job did not return exactly one container.")
            results[container["output_name"]] = archive.read(images[0])
    return results


def _download(data, filename, mimetype):
    return Response(data, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _encrypted_download(results):
    if len(results) == 1:
        name, payload = next(iter(results.items()))
        return _download(payload, name, "application/octet-stream")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in results.items():
            archive.writestr(name, payload)
    return _download(output.getvalue(), "gta6-money-encrypted.zip", "application/zip")


@gta6_bp.route("/gta6", methods=["GET", "POST"])
@login_required
async def gta6():
    _cleanup_old()
    if request.method == "GET":
        return await render_template("gta6.html", saves=None)

    form = await request.form
    action = form.get("action")
    try:
        if action == "load":
            upload = (await request.files).get("backup")
            if not upload or not upload.filename:
                raise SaveError("Choose a decrypted ZIP or JSAV file.")
            WORK_ROOT.mkdir(parents=True, exist_ok=True)
            work = WORK_ROOT / uuid.uuid4().hex
            work.mkdir()
            try:
                input_path = work / "input"
                await upload.save(input_path)
                if input_path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise SaveError("The upload exceeds the 128 MiB limit.")
                if zipfile.is_zipfile(input_path):
                    check_zip_safety(input_path)
                saves = _editor_model(input_path)
                (work / "owner").write_text(str(session["user_id"]))
                return await render_template("gta6.html", saves=saves, edit_id=work.name)
            except Exception:
                shutil.rmtree(work, ignore_errors=True)
                raise

        if action in ("save", "encrypt"):
            edit_id = form.get("edit_id", "")
            if not re.fullmatch(r"[0-9a-f]{32}", edit_id):
                raise SaveError("The editor session is invalid. Please upload the package again.")
            work = WORK_ROOT / edit_id
            if not work.is_dir() or (work / "owner").read_text() != str(session["user_id"]):
                raise SaveError("The editor session expired. Please upload the package again.")
            try:
                input_path = work / "input"
                bank = _amount(form.get("bank"), "Bank")
                cash = _amount(form.get("cash"), "Cash")
                character = int(form.get("character", "0"))
                data = input_path.read_bytes()
                if zipfile.is_zipfile(input_path):
                    edited = edit_zip(data, bank=bank, cash=cash, character=character)
                    if action == "encrypt":
                        return _encrypted_download(await _encrypt_containers(session["user_id"], edited))
                    return _download(edited, "gta6-money-edited.zip", "application/zip")
                edited = edit_bytes(data, bank=bank, cash=cash, character=character)
                return _download(edited, "gta6-money-edited.jsav", "application/octet-stream")
            finally:
                shutil.rmtree(work, ignore_errors=True)
        raise SaveError("Unknown editor action.")
    except (SaveError, DangerousFileError, zipfile.BadZipFile, OSError, ValueError) as error:
        await flash(str(error), "error")
        return await render_template("gta6.html", saves=None), 400
