"""Search PS5 system library functions from the functions database."""

import os

import aiosqlite

DB_PATH = os.getenv("FUNCS_DB_PATH", "functions.db")


async def search_functions(query: str, limit: int = 50) -> list[dict]:
    """Search functions by name. Returns list of {name, library}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            like = f"%{query}%"
            async with conn.execute(
                "SELECT name, library FROM functions "
                "WHERE name LIKE ? "
                "ORDER BY length(name) LIMIT ?",
                (like, limit),
            ) as cursor:
                results = await cursor.fetchall()

            return [dict(r) for r in results]
    except Exception:
        return []
