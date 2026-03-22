#!/usr/bin/env python3
"""Build filesystem search database from PS4/PS5 filesystem dump text files."""

import os
import re
import sqlite3
import sys

DB_PATH = os.getenv("FS_DB_PATH", "filesystem.db")


def parse_fs_file(filepath, platform):
    """Parse a filesystem dump file. Each line is like:
    /path/to/thing: type (size bytes)
    """
    entries = []
    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip()
            if not line or line.startswith("==="):
                continue
            # Match: /path: type (N bytes)  or  /path: [excluded]  or  /path: type
            m = re.match(r"^(/\S*?):\s+(.+)$", line)
            if not m:
                continue
            path = m.group(1)
            rest = m.group(2)

            # Parse type and size
            size_match = re.search(r"\((\d+)\s+bytes?\)", rest)
            size = int(size_match.group(1)) if size_match else None

            # Clean up the type description
            ftype = re.sub(r"\s*\(\d+\s+bytes?\)", "", rest).strip()

            entries.append((path, ftype, size, platform))
    return entries


def build():
    ps5_file = sys.argv[1] if len(sys.argv) > 1 else None
    ps4_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not ps5_file or not ps4_file:
        print("Usage: build_fs_db.py <ps5_fs.txt> <ps4_fs.txt>")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS filesystem")
    conn.execute("""
        CREATE TABLE filesystem (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            ftype TEXT NOT NULL,
            size INTEGER,
            platform TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX idx_fs_path ON filesystem(path)")
    conn.execute("CREATE INDEX idx_fs_filename ON filesystem(filename)")
    conn.execute("CREATE INDEX idx_fs_platform ON filesystem(platform)")

    total = 0
    for filepath, platform in [(ps5_file, "ps5"), (ps4_file, "ps4")]:
        entries = parse_fs_file(filepath, platform)
        print(f"  {platform.upper()}: {len(entries)} entries")
        for path, ftype, size, plat in entries:
            filename = os.path.basename(path) or path
            conn.execute(
                "INSERT INTO filesystem (path, filename, ftype, size, platform) VALUES (?, ?, ?, ?, ?)",
                (path, filename, ftype, size, plat),
            )
        total += len(entries)

    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()
    print(f"  Done. {total} entries in {DB_PATH}")


if __name__ == "__main__":
    build()
