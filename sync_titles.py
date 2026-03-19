#!/usr/bin/env python3
"""Sync PlayStation title database from andshrew/PlayStation-Titles.

Downloads PS4 and PS5 title JSON files from GitHub, parses them,
and stores title_id -> game name mappings in a SQLite database.

Run weekly via cron:
  0 3 * * 0 cd /opt/htos && .venv/bin/python sync_titles.py
"""

import json
import os
import sqlite3
import urllib.request

GITHUB_BASE = "https://raw.githubusercontent.com/andshrew/PlayStation-Titles/master/Json"
FILES = {
    "ps4": f"{GITHUB_BASE}/PS4_Titles.json",
    "ps5": f"{GITHUB_BASE}/PS5_Titles.json",
}
DB_PATH = os.getenv("TITLES_DB_PATH", "titles.db")


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS titles (
            title_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_id TEXT,
            region TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_titles_name ON titles(name)")
    conn.commit()


def download_json(url):
    print(f"  Downloading {url} ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def sync():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total = 0
    for platform, url in FILES.items():
        data = download_json(url)
        print(f"  {platform.upper()}: {len(data)} entries")

        batch = []
        for entry in data:
            raw_id = entry.get("titleId", "")
            # titleId is like "CUSA00001_00" — strip the suffix
            title_id = raw_id.split("_")[0] if "_" in raw_id else raw_id
            name = entry.get("name", "").strip()
            if not title_id or not name:
                continue
            batch.append((
                title_id,
                name,
                platform,
                entry.get("contentId", ""),
                entry.get("region", ""),
            ))

        conn.executemany(
            "INSERT OR REPLACE INTO titles (title_id, name, platform, content_id, region) "
            "VALUES (?, ?, ?, ?, ?)",
            batch,
        )
        conn.commit()
        total += len(batch)

    conn.execute("PRAGMA optimize")
    conn.close()
    print(f"  Done. {total} titles synced to {DB_PATH}")


if __name__ == "__main__":
    sync()
