import asyncio
import json
import secrets

import bcrypt
from quart import Blueprint, Response, jsonify, render_template, request

from auth import admin_required
from models import get_db

admin_web_bp = Blueprint("admin_web", __name__)


@admin_web_bp.route("/admin")
@admin_required
async def dashboard():
    db = await get_db()
    try:
        # Worker stats (combine current jobs + persisted stats)
        cursor = await db.execute(
            "SELECT wk.id, wk.name, wk.last_platform, wk.is_active, wk.jobs_completed, "
            "wk.suspended_until, wk.online_since, "
            "COALESCE(SUM(CASE WHEN j.status = 'done' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE(ps.hist_done, 0) as done, "
            "COALESCE(SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE(ps.hist_failed, 0) as failed, "
            "COUNT(j.id) + COALESCE(ps.hist_total, 0) as tracked, "
            "CASE WHEN wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-300 seconds') "
            "THEN 1 ELSE 0 END as is_online "
            "FROM worker_keys wk "
            "LEFT JOIN jobs j ON j.worker_key_id = wk.id "
            "LEFT JOIN ("
            "  SELECT worker_key_id, SUM(done) as hist_done, SUM(failed) as hist_failed, "
            "  SUM(total) as hist_total FROM job_stats GROUP BY worker_key_id"
            ") ps ON ps.worker_key_id = wk.id "
            "GROUP BY wk.id "
            "ORDER BY tracked DESC, wk.last_used DESC"
        )
        workers = [dict(r) for r in await cursor.fetchall()]

        # Recent jobs
        cursor = await db.execute(
            "SELECT j.id, u.username, j.operation, j.status, j.created_at, j.error, "
            "j.params, wk.name as worker_name "
            "FROM jobs j JOIN users u ON j.user_id = u.id "
            "LEFT JOIN worker_keys wk ON j.worker_key_id = wk.id "
            "ORDER BY j.created_at DESC LIMIT 30"
        )
        jobs = [dict(r) for r in await cursor.fetchall()]
        for j in jobs:
            if j["params"]:
                p = json.loads(j["params"])
                j["game_title"] = p.get("game_title", "")
                j["title_id"] = p.get("title_id", "")
                if not j["game_title"] and not j["title_id"]:
                    j["game_title"] = p.get("savename", "")
            else:
                j["game_title"] = ""
                j["title_id"] = ""

        # Queue count
        cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'")
        queued = (await cursor.fetchone())[0]

        # Total users
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]

        # Total job stats (current + persisted)
        cursor = await db.execute(
            "SELECT "
            "COUNT(*) + COALESCE((SELECT SUM(total) FROM job_stats), 0) as total, "
            "COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE((SELECT SUM(done) FROM job_stats), 0) as done, "
            "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE((SELECT SUM(failed) FROM job_stats), 0) as failed, "
            "COALESCE(SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END), 0) as active "
            "FROM jobs"
        )
        stats = dict(await cursor.fetchone())

        # Per-operation stats (current + persisted)
        cursor = await db.execute(
            "SELECT operation, SUM(done) as done, SUM(failed) as failed, SUM(total) as total FROM ("
            "  SELECT operation, "
            "    SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done, "
            "    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            "    COUNT(*) as total "
            "  FROM jobs GROUP BY operation "
            "  UNION ALL "
            "  SELECT operation, done, failed, total FROM job_stats"
            ") GROUP BY operation ORDER BY total DESC"
        )
        op_stats = [dict(r) for r in await cursor.fetchall()]

    finally:
        await db.close()

    for w in workers:
        total = w["done"] + w["failed"]
        w["total"] = total
        w["rate"] = f"{100 * w['done'] // total}%" if total > 0 else "-"

    completed = stats["done"] + stats["failed"]
    stats["rate"] = f"{100 * stats['done'] // completed}%" if completed > 0 else "-"
    for op in op_stats:
        op_completed = op["done"] + op["failed"]
        op["rate"] = f"{100 * op['done'] // op_completed}%" if op_completed > 0 else "-"

    return await render_template(
        "admin.html",
        workers=workers,
        jobs=jobs,
        queued=queued,
        total_users=total_users,
        stats=stats,
        op_stats=op_stats,
    )


@admin_web_bp.route("/admin/stats")
@admin_required
async def stats_json():
    """JSON endpoint for refreshing dashboard stats."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'")
        queued = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT "
            "COUNT(*) + COALESCE((SELECT SUM(total) FROM job_stats), 0) as total, "
            "COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE((SELECT SUM(done) FROM job_stats), 0) as done, "
            "COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE((SELECT SUM(failed) FROM job_stats), 0) as failed "
            "FROM jobs"
        )
        stats = dict(await cursor.fetchone())

        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM worker_keys wk "
            "WHERE wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-300 seconds') "
            "AND wk.is_active = 1 AND (wk.suspended_until IS NULL OR wk.suspended_until <= datetime('now'))"
        )
        workers_online = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT operation, SUM(done) as done, SUM(failed) as failed, SUM(total) as total FROM ("
            "  SELECT operation, "
            "    SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done, "
            "    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            "    COUNT(*) as total "
            "  FROM jobs GROUP BY operation "
            "  UNION ALL "
            "  SELECT operation, done, failed, total FROM job_stats"
            ") GROUP BY operation ORDER BY total DESC"
        )
        op_stats = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    completed = stats["done"] + stats["failed"]
    stats["rate"] = f"{100 * stats['done'] // completed}%" if completed > 0 else "-"
    for op in op_stats:
        op_completed = op["done"] + op["failed"]
        op["rate"] = f"{100 * op['done'] // op_completed}%" if op_completed > 0 else "-"

    return {
        "queued": queued,
        "total_users": total_users,
        "workers_online": workers_online,
        "stats": stats,
        "op_stats": op_stats,
    }


@admin_web_bp.route("/admin/feed")
@admin_required
async def feed():
    """SSE endpoint that streams new/updated jobs to the admin dashboard."""
    last_seen = None

    async def generate():
        nonlocal last_seen
        # Get initial latest job timestamp
        db = await get_db()
        try:
            cursor = await db.execute("SELECT MAX(created_at) as latest FROM jobs")
            row = await cursor.fetchone()
            last_seen = row["latest"] if row and row["latest"] else "2000-01-01"
        finally:
            await db.close()

        while True:
            await asyncio.sleep(3)
            try:
                db = await get_db()
                try:
                    # New jobs since last check
                    cursor = await db.execute(
                        "SELECT j.id, u.username, j.operation, j.status, j.created_at, j.error, "
                        "j.params, wk.name as worker_name "
                        "FROM jobs j JOIN users u ON j.user_id = u.id "
                        "LEFT JOIN worker_keys wk ON j.worker_key_id = wk.id "
                        "WHERE j.created_at > ? "
                        "ORDER BY j.created_at ASC",
                        (last_seen,),
                    )
                    new_jobs = [dict(r) for r in await cursor.fetchall()]
                    for nj in new_jobs:
                        if nj.get("params"):
                            p = json.loads(nj["params"])
                            nj["game_title"] = p.get("game_title", "")
                            nj["title_id"] = p.get("title_id", "")
                            if not nj["game_title"] and not nj["title_id"]:
                                nj["game_title"] = p.get("savename", "")
                        else:
                            nj["game_title"] = ""
                            nj["title_id"] = ""
                        del nj["params"]

                    # Also check for status updates on recent jobs
                    cursor = await db.execute(
                        "SELECT j.id, j.status, j.error, wk.name as worker_name "
                        "FROM jobs j "
                        "LEFT JOIN worker_keys wk ON j.worker_key_id = wk.id "
                        "WHERE j.status IN ('done', 'failed', 'running') "
                        "ORDER BY j.created_at DESC LIMIT 50"
                    )
                    updates = [dict(r) for r in await cursor.fetchall()]

                    # Queue count
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
                    )
                    queued = (await cursor.fetchone())[0]
                finally:
                    await db.close()

                if new_jobs:
                    last_seen = new_jobs[-1]["created_at"]
                    yield f"data: {json.dumps({'type': 'new_jobs', 'jobs': new_jobs, 'queued': queued})}\n\n"
                elif updates:
                    yield f"data: {json.dumps({'type': 'updates', 'updates': updates, 'queued': queued})}\n\n"

            except Exception:
                pass

    return Response(generate(), content_type="text/event-stream")


@admin_web_bp.route("/admin/reset-user", methods=["POST"])
@admin_required
async def reset_user():
    data = await request.get_json()
    username = (data or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username required"}), 400

    token = secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()

    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE users SET reset_code = ? WHERE username = ?",
            (token_hash, username),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return jsonify({"ok": False, "error": f"User '{username}' not found"}), 404
    finally:
        await db.close()

    return jsonify({"ok": True, "url": f"/reset/{token}"})
