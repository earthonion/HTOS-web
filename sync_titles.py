#!/usr/bin/env python3
"""Sync PlayStation title database from andshrew/PlayStation-Titles.

Downloads PS4 and PS5 title JSON files from GitHub, parses them,
and stores title_id -> game name mappings in a SQLite database.

Run weekly via cron:
  0 3 * * 0 cd /opt/htos && .venv/bin/python sync_titles.py
"""

import asyncio
import json
import os
import urllib.request

import aiosqlite

GITHUB_BASE = (
    "https://raw.githubusercontent.com/andshrew/PlayStation-Titles/master/Json"
)
FILES = {
    "ps4": f"{GITHUB_BASE}/PS4_Titles.json",
    "ps5": f"{GITHUB_BASE}/PS5_Titles.json",
}
DB_PATH = os.getenv("TITLES_DB_PATH", "titles.db")

REGION_MAP = {
    # US
    "US": "US",
    "UP": "US",
    "UM": "US",
    "UT": "US",
    "UB": "US",
    "UA": "US",
    # EU
    "EU": "EU",
    "EP": "EU",
    "EM": "EU",
    "ET": "EU",
    "EB": "EU",
    # AS
    "AS": "AS",
    "HA": "AS",
    "HT": "AS",
    "HP": "AS",
    "HB": "AS",
    # JP
    "JP": "JP",
    "JA": "JP",
    "JB": "JP",
    # KR
    "KR": "KR",
    "KP": "KR",
    # Internal/Unknown
    "IP": "Internal",
}


async def init_db(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS titles (
            title_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_id TEXT,
            concept_id INTEGER,
            region TEXT
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_titles_name ON titles(name)")
    # Migration: add concept_id column if missing
    try:
        await conn.execute("ALTER TABLE titles ADD COLUMN concept_id INTEGER")
    except Exception:
        pass
    await conn.commit()


def download_json(url):
    print(f"  Downloading {url} ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


async def sync():
    async with aiosqlite.connect(DB_PATH) as conn:
        await init_db(conn)

        total = 0
        for platform, url in FILES.items():
            # Note: download_json is still synchronous, which is fine for a cron script.
            data = download_json(url)
            print(f"  {platform.upper()}: {len(data)} entries")

            batch = []
            for entry in data:
                raw_id = entry.get("titleId", "")
                # titleId is like "CUSA00001_00" — strip the suffix
                title_id = raw_id.partition("_")[0]
                name = entry.get("name", "").strip()
                content_id = entry.get("contentId", "")
                concept_id = entry.get("conceptId")
                raw_region = entry.get("region", "")

                if not title_id or not name or not content_id or not raw_region:
                    continue

                region = (
                    REGION_MAP.get(raw_region.strip().upper(), raw_region)
                    if raw_region
                    else ""
                )

                batch.append(
                    (
                        title_id,
                        name,
                        platform,
                        content_id,
                        concept_id,
                        region,
                    )
                )

            await conn.executemany(
                "INSERT OR REPLACE INTO titles (title_id, name, platform, content_id, concept_id, region) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            await conn.commit()
            total += len(batch)

        await conn.execute("PRAGMA optimize")

    print(f"  Done. {total} titles synced to {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(sync())
