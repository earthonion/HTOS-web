"""Auto-capture sample saves for the sample save database."""

import os
import shutil
import struct
import zipfile

from models import get_db
from services.titles import lookup_title_info

SAMPLES_DIR = os.path.join("workspace", "savedb_samples")


async def maybe_store_sample_from_dir(title_id: str, save_dir: str, platform: str):
    """Store a sample save from a directory (encrypt flow) if we don't have one yet."""
    if not title_id or not os.path.isdir(save_dir):
        return

    save_dir_name = _read_savedata_directory(save_dir) or ""

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM sample_saves WHERE title_id = ? AND save_dir_name = ?",
            (title_id, save_dir_name),
        )
        if await cursor.fetchone():
            return

        os.makedirs(SAMPLES_DIR, exist_ok=True)
        zip_name = (
            f"{title_id}_{save_dir_name}.zip" if save_dir_name else f"{title_id}.zip"
        )
        zip_path = os.path.join(SAMPLES_DIR, zip_name)
        if os.path.exists(zip_path):
            return

        # Detect save type before compressing
        save_type = detect_save_type(save_dir)

        # Copy to temp dir, zero account ID there, then compress
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(save_dir, os.path.join(tmp, "save"), dirs_exist_ok=True)
            tmp_save = os.path.join(tmp, "save")
            _zero_account_id(tmp_save, platform)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA) as zf:
                for root, _, files in os.walk(tmp_save):
                    for f in files:
                        full = os.path.join(root, f)
                        arcname = os.path.relpath(full, tmp_save)
                        zf.write(full, arcname)

        info = await lookup_title_info(title_id)
        title = info["name"] if info else ""
        region = info.get("region", "") if info else ""

        await db.execute(
            "INSERT OR IGNORE INTO sample_saves "
            "(title_id, save_dir_name, title, platform, region, save_type, save_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title_id, save_dir_name, title, platform, region, save_type, zip_path),
        )
        await db.commit()
    except Exception:
        pass
    finally:
        await db.close()


async def maybe_store_sample_from_zip(title_id: str, result_zip: str, platform: str):
    """Store a sample save from a result zip (decrypt flow) if we don't have one yet."""
    if not title_id or not os.path.isfile(result_zip):
        return

    # Extract to temp dir first so we can read param.sfo
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(result_zip, "r") as zf:
            zf.extractall(tmp)

        save_dir_name = _read_savedata_directory(tmp) or ""

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id FROM sample_saves WHERE title_id = ? AND save_dir_name = ?",
                (title_id, save_dir_name),
            )
            if await cursor.fetchone():
                return

            os.makedirs(SAMPLES_DIR, exist_ok=True)
            zip_name = (
                f"{title_id}_{save_dir_name}.zip"
                if save_dir_name
                else f"{title_id}.zip"
            )
            zip_path = os.path.join(SAMPLES_DIR, zip_name)
            if os.path.exists(zip_path):
                return

            _zero_account_id(tmp, platform)
            save_type = detect_save_type(tmp)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA) as zf:
                for root, _, files in os.walk(tmp):
                    for f in files:
                        full = os.path.join(root, f)
                        arcname = os.path.relpath(full, tmp)
                        zf.write(full, arcname)

            info = await lookup_title_info(title_id)
            title = info["name"] if info else ""
            region = info.get("region", "") if info else ""

            await db.execute(
                "INSERT OR IGNORE INTO sample_saves "
                "(title_id, save_dir_name, title, platform, region, save_type, save_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title_id, save_dir_name, title, platform, region, save_type, zip_path),
            )
            await db.commit()
        except Exception:
            pass
        finally:
            await db.close()


def _read_savedata_directory(save_dir: str) -> str | None:
    """Parse SAVEDATA_DIRECTORY from param.sfo in the given save directory."""
    for root, _, files in os.walk(save_dir):
        for f in files:
            if f.lower() == "param.sfo":
                path = os.path.join(root, f)
                try:
                    return _parse_sfo_key(path, "SAVEDATA_DIRECTORY")
                except Exception:
                    pass
    return None


def _parse_sfo_key(path: str, key: str) -> str | None:
    """Read a string value from a PSF (param.sfo) file by key name."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"\x00PSF":
            return None
        f.read(4)  # version
        key_offset = struct.unpack("<I", f.read(4))[0]
        data_offset = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<I", f.read(4))[0]

        entries = []
        for _ in range(count):
            k_off = struct.unpack("<H", f.read(2))[0]
            f.read(2)  # data_fmt
            data_len = struct.unpack("<I", f.read(4))[0]
            f.read(4)  # data_max_len
            d_off = struct.unpack("<I", f.read(4))[0]
            entries.append((k_off, d_off, data_len))

        for k_off, d_off, data_len in entries:
            f.seek(key_offset + k_off)
            k = b""
            while True:
                c = f.read(1)
                if c == b"\x00" or not c:
                    break
                k += c
            if k.decode("utf-8", errors="replace") == key:
                f.seek(data_offset + d_off)
                val = f.read(data_len)
                return val.rstrip(b"\x00").decode("utf-8", errors="replace")
    return None


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


def detect_save_type(save_dir: str) -> str:
    """Detect the likely save data format by examining non-SFO files."""
    signatures = []

    for root, _, files in os.walk(save_dir):
        # Skip sce_sys directory
        if "sce_sys" in root.split(os.sep):
            continue
        for fname in files:
            path = os.path.join(root, fname)
            try:
                sig = _identify_file(path, fname)
                if sig:
                    signatures.append(sig)
            except Exception:
                pass

    if not signatures:
        return "Binary"

    # Deduplicate and pick the most specific/common type
    counts = {}
    for s in signatures:
        counts[s] = counts.get(s, 0) + 1

    # Priority order: more specific types win
    priority = [
        "Unity",
        "Unreal",
        "Ren'Py",
        "Godot",
        "Lua",
        "Lua (KLEI)",
        "JSON",
        "XML",
        "SQLite",
        "Protobuf",
        "MessagePack",
        "PS2 VMC",
        "Trophy",
        "Text",
        "Binary",
    ]
    for p in priority:
        if p in counts:
            return p

    # Fallback: most common
    return max(counts, key=counts.get)


def _identify_file(path: str, fname: str) -> str | None:
    """Identify a single save file's format from its header and name."""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return None

        with open(path, "rb") as f:
            header = f.read(min(4096, size))
    except Exception:
        return None

    # --- Magic bytes ---

    # .NET Binary Formatter (Unity)
    if header[:5] == b"\x00\x01\x00\x00\x00":
        return "Unity"
    if b"Assembly-CSharp" in header[:512]:
        return "Unity"
    if b"UnityEngine" in header[:512]:
        return "Unity"

    # Unreal Engine save
    if header[:4] == b"GVAS":
        return "Unreal"

    # SQLite
    if header[:16] == b"SQLite format 3\x00":
        return "SQLite"

    # KLEI (Don't Starve / Klei Entertainment Lua saves)
    if header[:4] == b"KLEI":
        return "Lua (KLEI)"

    # Ren'Py (zip containing pickle + renpy_version)
    if header[:2] == b"PK":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                if "renpy_version" in names or "log" in names:
                    return "Ren'Py"
                # Check for pickle inside zip
                for n in names:
                    data = zf.read(n)
                    if data[:2] == b"\x80\x02" and b"renpy" in data[:200]:
                        return "Ren'Py"
        except Exception:
            pass
        return "Zip"

    # Godot resource
    if header[:4] in (b"RSRC", b"RSCC"):
        return "Godot"

    # PS2 Virtual Memory Card
    if header[:20] == b"Sony PS2 Memory Card" or header[:20] == b"Sony PS2 Memory ":
        return "PS2 VMC"

    # Trophy data
    if header[:8] == b"OlpsTrpD":
        return "Trophy"

    # MessagePack (common fixmap/fixarray/bin headers)
    if header[0] in (0xDE, 0xDF, 0xDC, 0xDD, 0xC4, 0xC5, 0xC6) and size > 16:
        # Heuristic: check if it looks like valid msgpack
        if not any(header[1:20]):  # too many nulls = probably not msgpack
            pass
        else:
            return "MessagePack"

    # Protobuf (heuristic: starts with field tag, typically 0x08-0x7A)
    # Too unreliable on its own, skip unless file extension hints at it
    if fname.endswith((".pb", ".proto", ".protobuf")):
        return "Protobuf"

    # --- Text-based formats ---
    # Try to decode as text
    try:
        text = header[:2048].decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        text = None

    if text:
        stripped = text.lstrip()

        # JSON
        if stripped[:1] in ("{", "["):
            try:
                import json

                json.loads(header.decode("utf-8", errors="replace"))
                return "JSON"
            except Exception:
                # Might be truncated but still JSON-like
                if stripped[:1] == "{" and ":" in stripped[:200]:
                    return "JSON"
                if stripped[:1] == "[" and (
                    "{" in stripped[:200] or "," in stripped[:50]
                ):
                    return "JSON"

        # XML
        if (
            stripped.startswith("<?xml")
            or stripped.startswith("<!")
            or (stripped.startswith("<") and ">" in stripped[:200])
        ):
            return "XML"

        # Lua source
        if any(
            kw in stripped[:500]
            for kw in (
                "function ",
                "local ",
                "return {",
                "end\n",
                "require(",
            )
        ):
            return "Lua"

        # INI/config
        if stripped.startswith("[") and "]" in stripped[:100] and "=" in stripped[:200]:
            return "INI"

        # CSV-like
        if stripped.count(",") > 5 and stripped.count("\n") > 2:
            return "CSV"

        # Generic text
        return "Text"

    # Check entropy to distinguish encrypted from plain binary
    if _is_high_entropy(header):
        return "Encrypted"

    return "Binary"


def _is_high_entropy(data: bytes) -> bool:
    """Return True if data has high Shannon entropy (likely encrypted/compressed)."""
    import math

    if len(data) < 64:
        return False
    counts = [0] * 256
    for b in data[:4096]:
        counts[b] += 1
    length = min(len(data), 4096)
    entropy = 0.0
    for c in counts:
        if c:
            p = c / length
            entropy -= p * math.log2(p)
    # 7.5+ bits per byte = very likely encrypted/compressed (max is 8.0)
    return entropy >= 7.5
