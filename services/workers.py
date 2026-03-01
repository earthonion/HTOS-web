from models import get_db


async def ps5_workers_online():
    """Check if any PS5 worker has polled within the last 90 seconds."""
    try:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = 'last_ps5_worker' "
                "AND value > datetime('now', '-90 seconds')"
            )
            row = await cursor.fetchone()
            return row is not None
        finally:
            await db.close()
    except Exception:
        return False
