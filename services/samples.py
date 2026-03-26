"""Auto-capture sample saves for the sample save database."""

import os
import shutil
import zipfile

from models import get_db
from services.titles import lookup_title_info

SAMPLES_DIR = os.path.join("workspace", "savedb_samples")


async def maybe_store_sample_from_dir(title_id: str, save_dir: str, platform: str):
    """Store a sample save from a directory (encrypt flow) if we don't have one yet."""
    if not title_id or not os.path.isdir(save_dir):
        return

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sample_saves WHERE title_id = ?", (title_id,)
        )
        if await cursor.fetchone():
            return

        os.makedirs(SAMPLES_DIR, exist_ok=True)
        dest = os.path.join(SAMPLES_DIR, title_id)
        if os.path.exists(dest):
            return

        shutil.copytree(save_dir, dest)
        _zero_account_id(dest, platform)

        info = await lookup_title_info(title_id)
        title = info["name"] if info else ""
        region = info.get("region", "") if info else ""

        await db.execute(
            "INSERT OR IGNORE INTO sample_saves (title_id, title, platform, region, save_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (title_id, title, platform, region, dest),
        )
        await db.commit()
    except Exception:
        pass
    finally:
        await db.close()


async def maybe_store_sample_from_zip(title_id: str, zip_path: str, platform: str):
    """Store a sample save from a result zip (decrypt flow) if we don't have one yet."""
    if not title_id or not os.path.isfile(zip_path):
        return

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sample_saves WHERE title_id = ?", (title_id,)
        )
        if await cursor.fetchone():
            return

        os.makedirs(SAMPLES_DIR, exist_ok=True)
        dest = os.path.join(SAMPLES_DIR, title_id)
        if os.path.exists(dest):
            return

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest)

        _zero_account_id(dest, platform)

        info = await lookup_title_info(title_id)
        title = info["name"] if info else ""
        region = info.get("region", "") if info else ""

        await db.execute(
            "INSERT OR IGNORE INTO sample_saves (title_id, title, platform, region, save_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (title_id, title, platform, region, dest),
        )
        await db.commit()
    except Exception:
        pass
    finally:
        await db.close()


def _zero_account_id(save_dir: str, platform: str):
    """Zero the account ID in param.sfo so the sample is generic."""
    for root, _, files in os.walk(save_dir):
        for f in files:
            if f.lower() == "param.sfo":
                path = os.path.join(root, f)
                try:
                    with open(path, "r+b") as fh:
                        data = fh.read()
                        if len(data) > 0x1C0 and data[:4] == b"\x00PSF":
                            offset = 0x1B8 if platform == "ps5" else 0x15C
                            fh.seek(offset)
                            fh.write(b"\x00" * 8)
                except Exception:
                    pass
