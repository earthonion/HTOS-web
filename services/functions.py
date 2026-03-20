"""Search PS5 system library functions from the functions database."""

import os
import sqlite3

DB_PATH = os.getenv("FUNCS_DB_PATH", "functions.db")


def search_functions(query: str, limit: int = 50) -> list[dict]:
    """Search functions by name. Returns list of {name, library}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        like = f"%{query}%"
        cursor = conn.execute(
            "SELECT name, library FROM functions "
            "WHERE name LIKE ? "
            "ORDER BY length(name) LIMIT ?",
            (like, limit),
        )
        results = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return results
    except Exception:
        return []
