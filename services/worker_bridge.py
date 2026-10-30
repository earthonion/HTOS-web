"""Fallback bridge worker for PS4 decrypt/encrypt when no console workers are online."""

import asyncio
import json
import logging
import os
import zipfile

import httpx

from config import RESULT_DIR
from services.jobs import Job, push_log

log = logging.getLogger("worker_bridge")

BRIDGE_URL = os.getenv("BRIDGE_URL", "").rstrip("/")
BRIDGE_KEY = os.getenv("BRIDGE_KEY", "")

POLL_INTERVAL = 2  # seconds


def is_configured() -> bool:
    return bool(BRIDGE_URL)


def _headers() -> dict:
    h = {}
    if BRIDGE_KEY:
        h["X-Api-Key"] = BRIDGE_KEY
    return h


async def check_available() -> bool:
    """Check if bridge is reachable and has capacity."""
    if not is_configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{BRIDGE_URL}/api/status")
            if r.status_code == 200:
                data = r.json()
                return data.get("available_jobs", 0) > 0
    except Exception:
        pass
    return False


async def process_decrypt(job: Job):
    """Send decrypt job to bridge API."""
    upload_dir = job.params.get("upload_dir", "")
    if not upload_dir or not os.path.isdir(upload_dir):
        raise RuntimeError("No upload directory found")

    # Find save/sealed_key pairs: SAVENAME + SAVENAME.bin
    files_in_dir = os.listdir(upload_dir)
    bin_files = [f for f in files_in_dir if f.endswith(".bin")]

    if not bin_files:
        raise RuntimeError("No .bin (sealed key) files found")

    results = []
    for bin_name in bin_files:
        save_name = bin_name[:-4]  # strip .bin
        save_path = os.path.join(upload_dir, save_name)
        bin_path = os.path.join(upload_dir, bin_name)

        if not os.path.isfile(save_path):
            push_log(
                job.job_id, "WARNING", f"No matching save file for {bin_name}, skipping"
            )
            continue

        push_log(job.job_id, "INFO", f"Sending {save_name} to fallback decryptor...")

        async with httpx.AsyncClient(timeout=300) as client:
            with open(bin_path, "rb") as sk, open(save_path, "rb") as sf:
                r = await client.post(
                    f"{BRIDGE_URL}/api/decrypt",
                    headers=_headers(),
                    files={
                        "sealed_key": (bin_name, sk, "application/octet-stream"),
                        "save_file": (save_name, sf, "application/octet-stream"),
                    },
                )

            if r.status_code != 200:
                err = (
                    r.json().get("error", r.text)
                    if r.headers.get("content-type", "").startswith("application/json")
                    else r.text
                )
                raise RuntimeError(f"Decrypt submit failed: {err}")

            st_job_id = r.json()["job_id"]
            push_log(job.job_id, "INFO", f"Decrypting {save_name}...")

            # Poll for completion
            result_url = await _poll_job(client, st_job_id, job.job_id)

            # Download result
            push_log(job.job_id, "INFO", f"Downloading decrypted {save_name}...")
            dl = await client.get(
                f"{BRIDGE_URL}{result_url}",
                headers=_headers(),
                timeout=120,
            )
            if dl.status_code != 200:
                raise RuntimeError(f"Download failed for {save_name}")

            results.append((save_name, dl.content))

    if not results:
        raise RuntimeError("No saves were successfully decrypted")

    # Package all results into a single zip
    result_path = os.path.join(RESULT_DIR, f"{job.job_id}.zip")
    with zipfile.ZipFile(result_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
        for save_name, data in results:
            # Result may be a zip — merge its contents
            import io

            try:
                with zipfile.ZipFile(io.BytesIO(data), "r") as inner:
                    for entry in inner.infolist():
                        if not entry.is_dir():
                            out_zip.writestr(entry.filename, inner.read(entry.filename))
            except zipfile.BadZipFile:
                # Not a zip, write raw
                out_zip.writestr(save_name, data)

    return result_path


async def process_encrypt(job: Job):
    """Send encrypt job to bridge API."""
    upload_dir = job.params.get("upload_dir", "")
    if not upload_dir or not os.path.isdir(upload_dir):
        raise RuntimeError("No upload directory found")

    account_id = job.params.get("account_id", "")
    saveblocks = job.params.get("saveblocks", 0)

    # Build a zip of the save directory
    import io

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
        for root, _dirs, files in os.walk(upload_dir):
            for f in files:
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, upload_dir)
                zf.write(filepath, arcname)
    zip_buf.seek(0)

    params = {}
    if account_id:
        params["account_id"] = account_id
    if saveblocks:
        params["num_blocks"] = saveblocks

    push_log(job.job_id, "INFO", "Sending to fallback encryptor...")

    async with httpx.AsyncClient(timeout=300) as client:
        form_files = {
            "save_file": ("save.zip", zip_buf, "application/zip"),
        }
        form_data = {}
        if params:
            form_data["params"] = json.dumps(params)

        r = await client.post(
            f"{BRIDGE_URL}/api/encrypt",
            headers=_headers(),
            files=form_files,
            data=form_data,
        )

        if r.status_code != 200:
            err = (
                r.json().get("error", r.text)
                if r.headers.get("content-type", "").startswith("application/json")
                else r.text
            )
            raise RuntimeError(f"Encrypt submit failed: {err}")

        st_job_id = r.json()["job_id"]
        push_log(job.job_id, "INFO", "Encrypting save...")

        result_url = await _poll_job(client, st_job_id, job.job_id)

        push_log(job.job_id, "INFO", "Downloading encrypted save...")
        dl = await client.get(
            f"{BRIDGE_URL}{result_url}",
            headers=_headers(),
            timeout=120,
        )
        if dl.status_code != 200:
            raise RuntimeError("Download failed")

    result_path = os.path.join(RESULT_DIR, f"{job.job_id}.zip")
    with open(result_path, "wb") as f:
        f.write(dl.content)

    return result_path


async def _poll_job(client: httpx.AsyncClient, st_job_id: str, our_job_id: str) -> str:
    """Poll bridge job until complete. Returns download URL."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        r = await client.get(
            f"{BRIDGE_URL}/api/job/{st_job_id}",
            headers=_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Job poll failed: {r.status_code}")

        data = r.json()
        status = data.get("status", "")

        # Forward logs
        for log_entry in data.get("logs", []):
            msg = log_entry.get("message", "")
            if msg:
                push_log(our_job_id, "INFO", msg)

        if status == "complete":
            url = data.get("download_url", f"/api/download/{st_job_id}")
            return url
        elif status == "error":
            raise RuntimeError(data.get("error", "Unknown error from bridge"))
