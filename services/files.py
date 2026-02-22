import os
import shutil
import zipfile
from config import UPLOAD_DIR, RESULT_DIR, MAX_SAVE_FILE_SIZE


def _extract_zips_in_dir(directory: str):
    """If any .zip files exist in the directory, extract them and remove the zip."""
    for name in os.listdir(directory):
        filepath = os.path.join(directory, name)
        if os.path.isfile(filepath) and name.lower().endswith(".zip"):
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


def _strip_sdimg_prefix(directory: str):
    """PS5 saves prefix sealed files with 'sdimg_'. Rename to match the .bin counterparts."""
    for name in os.listdir(directory):
        if name.startswith("sdimg_"):
            stripped = name[6:]  # remove "sdimg_"
            src = os.path.join(directory, name)
            dst = os.path.join(directory, stripped)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.rename(src, dst)


async def save_uploaded_files(files, user_id: int, job_id: str) -> str:
    """Save uploaded files to uploads/<user_id>/<job_id>/. Returns the upload directory path.
    If a .zip is uploaded, it will be auto-extracted."""
    upload_dir = os.path.join(UPLOAD_DIR, str(user_id), job_id)
    os.makedirs(upload_dir, exist_ok=True)

    for f in files:
        filepath = os.path.join(upload_dir, f.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        await f.save(filepath)

    # Auto-extract any uploaded zip files and flatten nested dirs
    _extract_zips_in_dir(upload_dir)
    _flatten_single_subdirs(upload_dir)
    _strip_sdimg_prefix(upload_dir)
    _check_file_sizes(upload_dir)

    return upload_dir


async def save_uploaded_files_to(files, dest_dir: str) -> str:
    """Save uploaded files to a specific directory."""
    os.makedirs(dest_dir, exist_ok=True)
    for f in files:
        filepath = os.path.join(dest_dir, f.filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        await f.save(filepath)
    return dest_dir


def extract_account_id(directory: str) -> str | None:
    """Find param.sfo in directory and read account ID (8 bytes at 0x15C)."""
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower() == "param.sfo":
                sfo_path = os.path.join(root, f)
                return _read_account_id_from_sfo(sfo_path)
    return None


def extract_account_id_from_zip(zip_path: str) -> str | None:
    """Find param.sfo inside a zip and read account ID (8 bytes at 0x15C)."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith("param.sfo"):
                    data = zf.read(name)
                    if len(data) > 0x163:
                        return data[0x15C:0x164][::-1].hex()
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def _read_account_id_from_sfo(sfo_path: str) -> str | None:
    """Read account ID (8 bytes at 0x15C) from a param.sfo file."""
    try:
        with open(sfo_path, "rb") as fh:
            fh.seek(0x15C)
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
