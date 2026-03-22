"""Look up game titles by title ID from the synced titles database."""

import os

import aiosqlite

DB_PATH = os.getenv("TITLES_DB_PATH", "titles.db")


async def lookup_title(title_id: str) -> str | None:
    """Return game name for a title ID (e.g. 'CUSA03474'), or None."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute(
                "SELECT name FROM titles WHERE title_id = ? LIMIT 1",
                (title_id.upper(),),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception:
        return None


async def search_titles(query: str, limit: int = 10) -> list[dict]:
    """Search titles by name or title_id. Returns list of {title_id, name, platform}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT DISTINCT title_id, name, platform FROM titles "
                "WHERE title_id LIKE ? OR name LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ) as cursor:
                results = await cursor.fetchall()

            return [dict(r) for r in results]
    except Exception:
        return []
