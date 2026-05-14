from models import get_db


async def _platform_workers_online(column):
    """True iff any worker instance with the given capability flag has polled
    within the last 300s. `column` is 'is_ps4_capable' or 'is_ps5_capable'."""
    try:
        db = await get_db()
        try:
            cursor = await db.execute(
                f"SELECT 1 FROM worker_instances wi "
                f"JOIN worker_keys wk ON wk.id = wi.worker_key_id "
                f"WHERE wi.{column} = 1 "
                f"  AND wi.last_seen > datetime('now', '-300 seconds') "
                f"  AND wk.is_active = 1 "
                f"  AND (wk.suspended_until IS NULL OR wk.suspended_until <= datetime('now')) "
                f"LIMIT 1"
            )
            row = await cursor.fetchone()
            return row is not None
        finally:
            await db.close()
    except Exception:
        return False


async def ps5_workers_online():
    """Any worker that has polled the ps5 queue recently (includes dual workers)."""
    return await _platform_workers_online("is_ps5_capable")


async def ps4_workers_online():
    """Any worker that has polled the ps4 queue recently (includes dual workers)."""
    return await _platform_workers_online("is_ps4_capable")
