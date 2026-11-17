#!/usr/bin/env python3
"""Delete uploaded files and results older than 7 days."""

import json
import os
import shutil
import sqlite3
import time

DIRS = [
    "workspace/uploads",
    "workspace/results",
    "workspace/processing",
]

MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds


def failed_job_paths():
    paths = set()
    with sqlite3.connect("/opt/htos/htos_web.db") as db:
        for (params,) in db.execute("SELECT params FROM jobs WHERE status = 'failed'"):
            try:
                values = json.loads(params or "{}")
            except json.JSONDecodeError:
                continue
            for key in ("upload_dir", "saves_dir", "sample_dir", "result_path"):
                value = values.get(key)
                if value:
                    paths.add(os.path.abspath(os.path.join("/opt/htos", value)))
    return paths


def cleanup():
    now = time.time()
    removed = 0
    preserved = failed_job_paths()

    for base in DIRS:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            path = os.path.abspath(os.path.join(base, name))
            if any(path == preserved_path or path.startswith(preserved_path + os.sep)
                   for preserved_path in preserved):
                continue
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
