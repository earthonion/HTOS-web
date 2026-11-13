from models import get_db


async def enforce_job_rate_limit(key):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, job_rate_limit FROM worker_keys WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        if not row or not row["job_rate_limit"]:
            return None, 0
        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE submitter_key_id = ? "
            "AND created_at > datetime('now', '-60 seconds')",
            (row["id"],),
        )
        count = (await cursor.fetchone())[0]
        return row["id"], 60 if count >= row["job_rate_limit"] else 0
    finally:
        await db.close()
