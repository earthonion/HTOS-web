"""Search PS4/PS5 filesystem paths from the filesystem database."""

import os
import sqlite3

DB_PATH = os.getenv("FS_DB_PATH", "filesystem.db")


def search_filesystem(query: str, platform: str = "", limit: int = 50) -> list[dict]:
    """Search filesystem entries by path or filename.
    Returns list of {path, filename, ftype, size, platform}."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        like = f"%{query}%"
        if platform and platform in ("ps4", "ps5"):
            cursor = conn.execute(
                "SELECT path, filename, ftype, size, platform FROM filesystem "
                "WHERE (path LIKE ? OR filename LIKE ?) AND platform = ? "
                "ORDER BY length(path) LIMIT ?",
                (like, like, platform, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT path, filename, ftype, size, platform FROM filesystem "
                "WHERE path LIKE ? OR filename LIKE ? "
                "ORDER BY length(path) LIMIT ?",
                (like, like, limit),
            )
        results = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return results
    except Exception:
        return []
