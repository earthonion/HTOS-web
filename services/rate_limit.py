import os

from models import get_db


ACTIVE_INSTALLATION_LIMIT = int(os.getenv("INSTALLATION_ACTIVE_JOB_LIMIT", "2"))
HOURLY_INSTALLATION_LIMIT = int(os.getenv("INSTALLATION_HOURLY_JOB_LIMIT", "120"))
DAILY_INSTALLATION_LIMIT = int(os.getenv("INSTALLATION_DAILY_JOB_LIMIT", "500"))
DAILY_INSTALLATION_BYTE_LIMIT = int(os.getenv("INSTALLATION_DAILY_BYTE_LIMIT", str(10 * 1024**3)))
DAILY_USER_LIMIT = int(os.getenv("INSTALLATION_USER_DAILY_JOB_LIMIT", "1000"))
DAILY_USER_BYTE_LIMIT = int(os.getenv("INSTALLATION_USER_DAILY_BYTE_LIMIT", str(20 * 1024**3)))
MAX_DISTINCT_IPS_PER_DAY = int(os.getenv("INSTALLATION_MAX_DISTINCT_IPS_PER_DAY", "25"))


async def enforce_job_rate_limit(key, incoming_bytes=0, client_ip=""):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, job_rate_limit, installation_id FROM worker_keys WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if not row:
            return None, 0
        key_id = row["id"]
        if row["job_rate_limit"]:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM jobs WHERE submitter_key_id = ? "
                "AND created_at > datetime('now', '-60 seconds')",
                (key_id,),
            )
            if (await cursor.fetchone())[0] >= row["job_rate_limit"]:
                return key_id, 60
        installation_id = row["installation_id"]
        if not installation_id:
            return key_id, 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE submitter_installation_id = ? "
            "AND status IN ('pending', 'queued', 'running')",
            (installation_id,),
        )
        if (await cursor.fetchone())[0] >= ACTIVE_INSTALLATION_LIMIT:
            return key_id, 30

        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE submitter_installation_id = ? "
            "AND created_at > datetime('now', '-1 hour')",
            (installation_id,),
        )
        if (await cursor.fetchone())[0] >= HOURLY_INSTALLATION_LIMIT:
            return key_id, 3600

        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(submitter_bytes), 0) FROM jobs "
            "WHERE submitter_installation_id = ? AND created_at > datetime('now', '-1 day')",
            (installation_id,),
        )
        count, uploaded = await cursor.fetchone()
        if count >= DAILY_INSTALLATION_LIMIT:
            return key_id, 86400
        if uploaded + max(0, incoming_bytes) > DAILY_INSTALLATION_BYTE_LIMIT:
            return key_id, 3600

        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(submitter_bytes), 0) FROM jobs "
            "WHERE user_id = ? AND submitter_installation_id IS NOT NULL "
            "AND created_at > datetime('now', '-1 day')",
            (row["user_id"],),
        )
        user_count, user_uploaded = await cursor.fetchone()
        if user_count >= DAILY_USER_LIMIT:
            return key_id, 86400
        if user_uploaded + max(0, incoming_bytes) > DAILY_USER_BYTE_LIMIT:
            return key_id, 3600

        if client_ip:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT submitter_ip) FROM jobs "
                "WHERE submitter_installation_id = ? AND created_at > datetime('now', '-1 day')",
                (installation_id,),
            )
            distinct_ips = (await cursor.fetchone())[0]
            if distinct_ips >= MAX_DISTINCT_IPS_PER_DAY:
                cursor = await db.execute(
                    "SELECT 1 FROM jobs WHERE submitter_installation_id = ? "
                    "AND submitter_ip = ? AND created_at > datetime('now', '-1 day') LIMIT 1",
                    (installation_id, client_ip),
                )
                if not await cursor.fetchone():
                    return key_id, 3600
        return key_id, 0
    finally:
        await db.close()
