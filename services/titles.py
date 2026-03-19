"""Look up game titles by title ID from the synced titles database."""

import os
import sqlite3

DB_PATH = os.getenv("TITLES_DB_PATH", "titles.db")


def lookup_title(title_id: str) -> str | None:
    """Return game name for a title ID (e.g. 'CUSA03474'), or None."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "SELECT name FROM titles WHERE title_id = ? LIMIT 1",
            (title_id.upper(),),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def search_titles(query: str, limit: int = 10) -> list[dict]:
    """Search titles by name or title_id. Returns list of {title_id, name, platform}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT DISTINCT title_id, name, platform FROM titles "
            "WHERE title_id LIKE ? OR name LIKE ? LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        )
        results = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return results
    except Exception:
        return []
