#!/usr/bin/env python3
"""Verify entitlement URLs via HEAD requests and remove invalid ones.

Checks unchecked entries in batches, marks valid ones as verified=1,
and deletes entries that return non-200 responses.

Run periodically via cron:
  */30 * * * * cd /opt/htos && .venv/bin/python verify_entitlements.py
"""

import asyncio
import os
import sqlite3

import httpx

DB_PATH = os.getenv("DATABASE_PATH", "htos_web.db")
BATCH_SIZE = 50
TIMEOUT = 10


async def verify_batch():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get unchecked entries
    rows = conn.execute(
        "SELECT id, package_url FROM entitlements "
        "WHERE verified IS NULL AND length(package_url) > 5 "
        "ORDER BY id LIMIT ?",
        (BATCH_SIZE,),
    ).fetchall()

    if not rows:
        print("No unchecked entries remaining.")
        conn.close()
        return

    print(f"Checking {len(rows)} entries...")

    valid_ids = []
    invalid_ids = []

    async with httpx.AsyncClient(verify=False, timeout=TIMEOUT) as client:
        for row in rows:
            eid = row["id"]
            url = row["package_url"]
            try:
                resp = await client.head(url)
                if resp.status_code == 200:
                    valid_ids.append(eid)
                else:
                    print(f"  INVALID ({resp.status_code}): id={eid} {url[:80]}")
                    invalid_ids.append(eid)
            except Exception as exc:
                print(f"  ERROR: id={eid} {exc}")
                invalid_ids.append(eid)

            # Small delay between requests
            await asyncio.sleep(0.2)

    if valid_ids:
        conn.executemany(
            "UPDATE entitlements SET verified = 1 WHERE id = ?",
            [(i,) for i in valid_ids],
        )
    if invalid_ids:
        conn.executemany(
            "DELETE FROM entitlements WHERE id = ?",
            [(i,) for i in invalid_ids],
        )
    conn.commit()
    conn.close()

    print(f"Done. {len(valid_ids)} valid, {len(invalid_ids)} removed.")


if __name__ == "__main__":
    asyncio.run(verify_batch())
