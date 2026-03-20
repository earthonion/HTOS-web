import json
import os
import shutil
import zipfile
from config import UPLOAD_DIR, RESULT_DIR, MAX_SAVE_FILE_SIZE, CHUNK_DIR

# Upload security
# Only block Windows/macOS executables and installers — not scripts or media
# that could legitimately appear in PS4/PS5 saves (lua, py, svg, html, etc.)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif",
    ".dll", ".sys", ".drv",
    ".app", ".dmg",
    ".deb", ".rpm",
}

# Max total decompressed size for zips (2GB)
MAX_ZIP_DECOMPRESSED = 2 * 1024 * 1024 * 1024
# Max compression ratio (zip bomb detection)
MAX_ZIP_RATIO = 100


class DangerousFileError(Exception):
    pass


def _safe_join(base_dir: str, untrusted_path: str) -> str:
    """Safely join base_dir with an untrusted path, preventing path traversal.
    Raises DangerousFileError if the result escapes base_dir."""
    # Normalize and strip leading slashes/backslashes
    cleaned = untrusted_path.replace("\\", "/")
    # Remove any path components that are ".." or absolute
    parts = [p for p in cleaned.split("/") if p and p != ".."]
    if not parts:
        raise DangerousFileError("Invalid filename.")
    safe_path = os.path.join(base_dir, *parts)
    # Final check: resolved path must be under base_dir
    real_base = os.path.realpath(base_dir)
    real_path = os.path.realpath(safe_path)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise DangerousFileError(f"Path traversal blocked: {untrusted_path}")
    return safe_path


def check_dangerous_files(directory):
    """Scan directory for blocked file extensions."""
    for root, dirs, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in BLOCKED_EXTENSIONS:
                raise DangerousFileError(
                    f"Blocked file type: {f}. Only PS4/PS5 save files are allowed."
                )


def check_zip_safety(zip_path):
    """Check a zip file for zip bombs before extraction."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            compressed = os.path.getsize(zip_path)
            decompressed = sum(info.file_size for info in zf.infolist())

            if decompressed > MAX_ZIP_DECOMPRESSED:
                raise DangerousFileError(
                    f"Zip decompressed size ({decompressed // (1024*1024)}MB) exceeds limit. "
                    f"Max allowed is {MAX_ZIP_DECOMPRESSED // (1024*1024)}MB."
                )

            if compressed > 0 and decompressed / compressed > MAX_ZIP_RATIO:
                raise DangerousFileError(
                    "Suspicious zip file detected (possible zip bomb). Upload rejected."
                )

            # Check for blocked files and path traversal inside zip
            for info in zf.infolist():
                # Block path traversal in zip member names
                if ".." in info.filename or info.filename.startswith("/"):
                    raise DangerousFileError(
                        f"Suspicious path in zip: {info.filename}. Upload rejected."
                    )
                ext = os.path.splitext(info.filename)[1].lower()
                if ext in BLOCKED_EXTENSIONS:
                    raise DangerousFileError(
                        f"Blocked file type in zip: {info.filename}. "
                        f"Only PS4/PS5 save files are allowed."
                    )
    except zipfile.BadZipFile:
        raise DangerousFileError("Invalid zip file.")


def _extract_zips_in_dir(directory: str):
    """If any .zip files exist in the directory, extract them and remove the zip."""
    for name in os.listdir(directory):
        filepath = os.path.join(directory, name)
        if os.path.isfile(filepath) and name.lower().endswith(".zip"):
            check_zip_safety(filepath)
            try:
                with zipfile.ZipFile(filepath, "r") as zf:
                    zf.extractall(directory)
                os.remove(filepath)
            except (zipfile.BadZipFile, OSError):
                pass  # Not a valid zip, leave as-is


def _flatten_single_subdirs(directory: str):
    """If extraction created nested dirs with save pairs, flatten them."""
    # Walk to find save file pairs and move them to the top level
    for root, dirs, files in os.walk(directory):
        if root == directory:
            continue
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(directory, f)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    # Clean up empty subdirs
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


class InvalidSaveFilesError(Exception):
    pass


class FileTooLargeError(Exception):
    def __init__(self, filename, size):
        self.filename = filename
        self.size = size
        mb = size / (1024 * 1024)
        limit_mb = MAX_SAVE_FILE_SIZE / (1024 * 1024)
        super().__init__(f"{filename} is {mb:.0f}MB, max is {limit_mb:.0f}MB")


def _check_file_sizes(directory: str):
    """Raise FileTooLargeError if any file exceeds MAX_SAVE_FILE_SIZE."""
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            size = os.path.getsize(path)
            if size > MAX_SAVE_FILE_SIZE:
                raise FileTooLargeError(name, size)


def validate_save_pairs(directory: str):
    """Check that the upload directory contains valid save pairs.
    Each save file should have a matching .bin companion.
    Raises InvalidSaveFilesError with a user-friendly message if not."""
    files = [n for n in os.listdir(directory) if os.path.isfile(os.path.join(directory, n))]
    if not files:
        raise InvalidSaveFilesError("No files found. Please upload save file pairs (.bin + matching save file) or a .zip.")
    # If there's a zip, that's fine — it will be extracted
    if any(f.lower().endswith(".zip") for f in files):
        return
    bin_files = {f[:-4] for f in files if f.lower().endswith(".bin")}
    save_files = [f for f in files if not f.lower().endswith(".bin")]
    if not save_files:
        raise InvalidSaveFilesError("No save files found. You uploaded only .bin files — please include the matching save files too.")
    if not bin_files:
        raise InvalidSaveFilesError("No .bin files found. Encrypted saves need a .bin companion file. Please upload save pairs (.bin + matching save file) or a .zip.")
    unmatched = [f for f in save_files if f not in bin_files]
    if unmatched and not bin_files:
        raise InvalidSaveFilesError(f"Missing .bin files for: {', '.join(unmatched[:3])}. Please upload the matching .bin companion files.")


def validate_createsave_files(directory: str):
    """Check that the upload contains sce_sys/param.sfo for create save."""
    has_sce_sys = False
    has_sfo = False
    for root, dirs, files in os.walk(directory):
        if os.path.basename(root).lower() == "sce_sys":
            has_sce_sys = True
            if any(f.lower() == "param.sfo" for f in files):
                has_sfo = True
                break
    if not has_sce_sys:
        raise InvalidSaveFilesError("No sce_sys folder found. Please upload a save folder or zip containing an sce_sys directory.")
    if not has_sfo:
        raise InvalidSaveFilesError("Missing param.sfo inside sce_sys. Please make sure your save folder includes sce_sys/param.sfo.")


def _strip_sdimg_prefix(directory: str):
    """PS5 saves prefix sealed files with 'sdimg_'. Rename to match the .bin counterparts."""
    for name in os.listdir(directory):
        if name.startswith("sdimg_"):
            stripped = name[6:]  # remove "sdimg_"
            src = os.path.join(directory, name)
            dst = os.path.join(directory, stripped)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.rename(src, dst)


async def resolve_chunked_uploads(upload_ids: list, user_id: int, job_id: str) -> str:
    """Move assembled files from chunk staging to the job's upload dir.
    Returns the upload directory path."""
    upload_dir = os.path.join(UPLOAD_DIR, str(user_id), job_id)
    os.makedirs(upload_dir, exist_ok=True)

    for uid in upload_ids:
        chunk_dir = os.path.join(CHUNK_DIR, uid)
        if not os.path.isdir(chunk_dir):
            continue
        meta_path = os.path.join(chunk_dir, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        filename = meta["filename"]
        src = _safe_join(chunk_dir, filename)
        dst = _safe_join(upload_dir, filename)
        if os.path.isfile(src):
            shutil.move(src, dst)
        # Clean up chunk dir
        shutil.rmtree(chunk_dir, ignore_errors=True)

    # Auto-extract zips, strip prefix, check sizes and dangerous files
    _extract_zips_in_dir(upload_dir)
    _strip_sdimg_prefix(upload_dir)
    check_dangerous_files(upload_dir)
    _check_file_sizes(upload_dir)

    return upload_dir


async def save_uploaded_files(files, user_id: int, job_id: str) -> str:
    """Save uploaded files to uploads/<user_id>/<job_id>/. Returns the upload directory path.
    If a .zip is uploaded, it will be auto-extracted."""
    upload_dir = os.path.join(UPLOAD_DIR, str(user_id), job_id)
    os.makedirs(upload_dir, exist_ok=True)

    for f in files:
        if not f.filename or f.filename.endswith('/'):
            continue
        filepath = _safe_join(upload_dir, f.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        await f.save(filepath)

    # Auto-extract any uploaded zip files, strip prefix, check sizes and dangerous files
    _extract_zips_in_dir(upload_dir)
    _strip_sdimg_prefix(upload_dir)
    check_dangerous_files(upload_dir)
    _check_file_sizes(upload_dir)

    return upload_dir


async def save_uploaded_files_to(files, dest_dir: str) -> str:
    """Save uploaded files to a specific directory."""
    os.makedirs(dest_dir, exist_ok=True)
    for f in files:
        if not f.filename or f.filename.endswith('/'):
            continue
        filepath = _safe_join(dest_dir, f.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        await f.save(filepath)
    return dest_dir


def detect_save_platform(filepath: str) -> str:
    """Detect PS4 vs PS5 by reading first byte of save file.
    PS4 saves start with 0x01, PS5 with 0x02.
    Returns 'ps4', 'ps5', or 'unknown'."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(4)
            if len(header) >= 1:
                if header[0] == 0x01:
                    return "ps4"
                elif header[0] == 0x02:
                    return "ps5"
    except (OSError, IOError):
        pass
    return "unknown"


def detect_platform_in_dir(directory: str) -> str:
    """Detect platform from save files in a directory.
    Checks for sdimg_ prefix (PS5) and first-byte detection.
    Returns 'ps4', 'ps5', or 'unknown'."""
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.endswith(".bin") or name.lower() == "param.sfo":
                continue
            # sdimg_ prefix is a strong PS5 indicator
            if name.startswith("sdimg_"):
                return "ps5"
            filepath = os.path.join(root, name)
            platform = detect_save_platform(filepath)
            if platform != "unknown":
                return platform
    return "unknown"


def extract_account_id(directory: str, platform: str = "ps4") -> str | None:
    """Find param.sfo in directory and read account ID.
    PS4: 8 bytes at 0x15C, PS5: 8 bytes at 0x1B8."""
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower() == "param.sfo":
                sfo_path = os.path.join(root, f)
                return _read_account_id_from_sfo(sfo_path, platform)
    return None


def extract_account_id_from_zip(zip_path: str, platform: str = "ps4") -> str | None:
    """Find param.sfo inside a zip and read account ID.
    PS4: 8 bytes at 0x15C, PS5: 8 bytes at 0x1B8."""
    offset = 0x1B8 if platform == "ps5" else 0x15C
    end = offset + 8
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith("param.sfo"):
                    data = zf.read(name)
                    if len(data) > end:
                        return data[offset:end][::-1].hex()
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def _read_account_id_from_sfo(sfo_path: str, platform: str = "ps4") -> str | None:
    """Read account ID from a param.sfo file.
    PS4: 8 bytes at 0x15C, PS5: 8 bytes at 0x1B8."""
    offset = 0x1B8 if platform == "ps5" else 0x15C
    try:
        with open(sfo_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(8)
            if len(data) == 8:
                return data[::-1].hex()
    except (OSError, IOError):
        pass
    return None


def create_result_zip(source_dir: str, job_id: str) -> str:
    """Zip the result directory and return the zip path."""
    zip_path = os.path.join(RESULT_DIR, f"{job_id}.zip")
    os.makedirs(RESULT_DIR, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, source_dir)
                zf.write(filepath, arcname)

    return zip_path


def cleanup_upload(user_id: int, job_id: str):
    """Remove the upload directory for a job."""
    upload_dir = os.path.join(UPLOAD_DIR, str(user_id), job_id)
    if os.path.exists(upload_dir):
        shutil.rmtree(upload_dir, ignore_errors=True)
