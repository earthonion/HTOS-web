import os
import shutil

import aiofiles
import aiofiles.os

from utils.constants import (
    UPLOAD_ENCRYPTED, UPLOAD_DECRYPTED, DOWNLOAD_DECRYPTED, PNG_PATH, KEYSTONE_PATH,
    DOWNLOAD_ENCRYPTED, PARAM_PATH, RANDOMSTRING_LENGTH, logger
)
from utils.extras import generate_random_string


def init_workspace() -> tuple[str, str, str, str, str, str, str]:
    """Obtains the local paths for an user, used when initializing a command that needs the local filesystem."""
    randomString = generate_random_string(RANDOMSTRING_LENGTH)
    newUPLOAD_ENCRYPTED = os.path.join(UPLOAD_ENCRYPTED, randomString)
    newUPLOAD_DECRYPTED = os.path.join(UPLOAD_DECRYPTED, randomString)
    newDOWNLOAD_ENCRYPTED = os.path.join(DOWNLOAD_ENCRYPTED, randomString)
    newPNG_PATH = os.path.join(PNG_PATH, randomString)
    newPARAM_PATH = os.path.join(PARAM_PATH, randomString)
    newDOWNLOAD_DECRYPTED = os.path.join(DOWNLOAD_DECRYPTED, randomString)
    newKEYSTONE_PATH = os.path.join(KEYSTONE_PATH, randomString)

    return newUPLOAD_ENCRYPTED, newUPLOAD_DECRYPTED, newDOWNLOAD_ENCRYPTED, newPNG_PATH, newPARAM_PATH, newDOWNLOAD_DECRYPTED, newKEYSTONE_PATH


async def cleanup_simple(clean_list: list[str] | None) -> None:
    """Used to cleanup after a command that does not utilize the ps4 (local only)."""
    if not clean_list:
        return
    for folderpath in clean_list:
        try:
            if await aiofiles.os.path.exists(folderpath):
                shutil.rmtree(folderpath)
        except OSError as e:
            logger.error(f"Error accessing {folderpath} when cleaning up (simple): {e}")
