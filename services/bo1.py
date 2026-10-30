"""Black Ops 1 PS4 save PRF crypto + editor/patch chain helpers.

Legacy path (patch_text supplied at submit time): decrypt job completes,
chain overwrites every .PRF with a fresh encrypted blob, queues encrypt.

Editor path (bo1_editor=True): decrypt job completes, chain unpacks the
result zip into a stage dir and stops. User edits individual PRFs via
/bo1/edit/<job_id>, then clicks Encrypt which queues the follow-up job.
"""
from __future__ import annotations
import json, os, re, zipfile
from pathlib import Path
from Crypto.Cipher import DES3

from models import get_db
from services.jobs import create_job

KEY = b"Md8ea20lPcftYwsl496q63x9"
IV = b"0Peyx825"
BLOCK = 8


def _encrypt(data: bytes) -> bytes:
    return DES3.new(KEY, DES3.MODE_CBC, IV).encrypt(data)


def _decrypt(data: bytes) -> bytes:
    return DES3.new(KEY, DES3.MODE_CBC, IV).decrypt(data)


def _pad(data: bytes) -> bytes:
    rem = len(data) % BLOCK
    return data + b"\x00" * (BLOCK - rem) if rem else data


def _parse_sets(text: str):
    entries = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^set\s+(\S+)\s+(.*)", line)
        if m:
            entries.append((m.group(1), m.group(2)))
    return entries


def build_prf_from_patch(patch_text: str) -> bytes:
    """Serialize `set key value` lines into an encrypted PRF blob."""
    entries = _parse_sets(patch_text)
    body = ("\n".join(f"set {k} {v}" for k, v in entries) + "\n").encode("latin-1")
    return _encrypt(_pad(body))


def decrypt_prf_bytes(blob: bytes) -> str:
    """Decrypt a PRF file's contents to text. Strips trailing NULs."""
    if len(blob) % BLOCK != 0:
        blob = blob[: len(blob) - (len(blob) % BLOCK)]
    plain = _decrypt(blob).rstrip(b"\x00")
    try:
        return plain.decode("latin-1")
    except UnicodeDecodeError:
        return plain.decode("latin-1", errors="replace")


def patch_prf_files(root: Path, patch_text: str) -> list[Path]:
    """Overwrite every .PRF (case-insensitive) under root with the encrypted
    patch blob. Returns the list of files touched. Used by legacy patch mode."""
    blob = build_prf_from_patch(patch_text)
    patched: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".prf":
            p.write_bytes(blob)
            patched.append(p)
    return patched


def write_prf_from_text(prf_path: Path, text: str) -> None:
    """Encrypt `set key value` text and write to the given .PRF path.
    Used by editor mode when the user hits Save on a single PRF."""
    prf_path.write_bytes(build_prf_from_patch(text))


def list_prf_files(root: Path) -> list[dict]:
    """List every .PRF file under root with size + relpath, for the browser."""
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".prf":
            out.append({
                "rel": str(p.relative_to(root)),
                "name": p.name,
                "size": p.stat().st_size,
            })
    return out


async def _stage_decrypted_zip(user_id: int, decrypt_job_id: str, result_zip: str) -> Path:
    """Extract the decrypted result zip into a per-decrypt-job stage dir the
    editor can walk. Returns the stage dir path."""
    stage = Path("workspace/uploads") / str(user_id) / f"bo1-edit-{decrypt_job_id}"
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(result_zip) as z:
        z.extractall(stage)
    return stage


async def chain_stage_only(*, decrypt_job_id: str, user_id: int, result_zip: str) -> str:
    """Editor-mode chain: extract the decrypted zip to a stage dir and record
    its path in the decrypt job's params so /bo1/edit/<id> can find it.

    Does NOT queue an encrypt job — the user drives that from the editor.
    Returns the stage dir path.
    """
    stage = await _stage_decrypted_zip(user_id, decrypt_job_id, result_zip)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT params FROM jobs WHERE id = ?", (decrypt_job_id,)
        )
        row = await cursor.fetchone()
        if row:
            jp = json.loads(row["params"] or "{}")
            jp["bo1_stage_dir"] = str(stage)
            await db.execute(
                "UPDATE jobs SET params = ? WHERE id = ?",
                (json.dumps(jp), decrypt_job_id),
            )
            await db.commit()
    finally:
        await db.close()

    return str(stage)


async def chain_after_decrypt(
    *,
    decrypt_job_id: str,
    user_id: int,
    result_zip: str,
    patch_text: str,
    target_account_id: str,
) -> str:
    """Legacy patch chain: extract, overwrite every .PRF with patch_text,
    queue an encrypt job re-signing to target_account_id.
    Returns the follow-up encrypt job id."""
    encrypt_job = await create_job(user_id, "encrypt", {}, ready=False)
    stage = Path("workspace/uploads") / str(user_id) / encrypt_job.job_id
    stage.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(result_zip) as z:
        z.extractall(stage)

    patched = patch_prf_files(stage, patch_text)

    params = {
        "upload_dir": str(stage),
        "platform": "ps4",
        "account_id": target_account_id,
        "bo1_source_job": decrypt_job_id,
    }
    await encrypt_job.update_params(params)
    if patched:
        encrypt_job.logger.info(
            f"BO1: patched {len(patched)} .PRF file(s), re-encrypting to "
            f"account {target_account_id[:8]}…"
        )
    else:
        encrypt_job.logger.warning(
            "BO1: no .PRF files found in the decrypted save — re-encrypting anyway."
        )
    await encrypt_job.set_status("queued")

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT params FROM jobs WHERE id = ?", (decrypt_job_id,)
        )
        row = await cursor.fetchone()
        if row:
            jp = json.loads(row["params"] or "{}")
            jp["bo1_encrypt_job"] = encrypt_job.job_id
            await db.execute(
                "UPDATE jobs SET params = ? WHERE id = ?",
                (json.dumps(jp), decrypt_job_id),
            )
            await db.commit()
    finally:
        await db.close()

    return encrypt_job.job_id


async def queue_encrypt_from_stage(
    *,
    user_id: int,
    stage_dir: str,
    target_account_id: str,
    decrypt_job_id: str | None = None,
) -> str:
    """Editor-mode: user is done editing PRFs in `stage_dir`; queue an
    encrypt job that re-signs the whole tree to target_account_id."""
    encrypt_job = await create_job(user_id, "encrypt", {}, ready=False)
    params = {
        "upload_dir": stage_dir,
        "platform": "ps4",
        "account_id": target_account_id,
    }
    if decrypt_job_id:
        params["bo1_source_job"] = decrypt_job_id
    await encrypt_job.update_params(params)
    encrypt_job.logger.info(
        f"BO1: re-encrypting edited PRF tree to account {target_account_id[:8]}…"
    )
    await encrypt_job.set_status("queued")

    if decrypt_job_id:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT params FROM jobs WHERE id = ?", (decrypt_job_id,)
            )
            row = await cursor.fetchone()
            if row:
                jp = json.loads(row["params"] or "{}")
                jp["bo1_encrypt_job"] = encrypt_job.job_id
                await db.execute(
                    "UPDATE jobs SET params = ? WHERE id = ?",
                    (json.dumps(jp), decrypt_job_id),
                )
                await db.commit()
        finally:
            await db.close()

    return encrypt_job.job_id
