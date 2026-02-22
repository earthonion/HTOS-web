#!/usr/bin/env python3
"""Delete uploaded files and results older than 7 days."""

import os
import shutil
import time

DIRS = [
    "workspace/uploads",
    "workspace/results",
    "workspace/processing",
]

MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds


def cleanup():
    now = time.time()
    removed = 0

    for base in DIRS:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            path = os.path.join(base, name)
            try:
                mtime = os.path.getmtime(path)
                if now - mtime > MAX_AGE:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    removed += 1
            except OSError:
                pass

    print(f"Cleaned up {removed} items older than 7 days.")


if __name__ == "__main__":
    cleanup()
