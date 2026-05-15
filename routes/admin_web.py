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
        # Worker stats (combine current jobs + persisted stats + instance count)
        cursor = await db.execute(
            "SELECT wk.id, wk.name, wk.last_platform, wk.is_active, wk.jobs_completed, "
            "wk.suspended_until, wk.online_since, "
            "COALESCE(SUM(CASE WHEN j.status = 'done' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE(ps.hist_done, 0) as done, "
            "COALESCE(SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END), 0) "
            "  + COALESCE(ps.hist_failed, 0) as failed, "
            "COUNT(j.id) + COALESCE(ps.hist_total, 0) as tracked, "
            "CASE WHEN wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-300 seconds') "
            "THEN 1 ELSE 0 END as is_online, "
            "COALESCE(ic.instance_count, 0) as instances, "
            "COALESCE(ic.online_count, 0) as online_instances, "
            "COALESCE(ic.ps4_online, 0) as ps4_online, "
            "COALESCE(ic.ps5_online, 0) as ps5_online "
            "FROM worker_keys wk "
            "LEFT JOIN jobs j ON j.worker_key_id = wk.id "
            "LEFT JOIN ("
            "  SELECT worker_key_id, SUM(done) as hist_done, SUM(failed) as hist_failed, "
            "  SUM(total) as hist_total FROM job_stats GROUP BY worker_key_id"
            ") ps ON ps.worker_key_id = wk.id "
            "LEFT JOIN ("
            "  SELECT worker_key_id, COUNT(*) as instance_count, "
            "    SUM(CASE WHEN last_seen > datetime('now','-300 seconds') THEN 1 ELSE 0 END) as online_count, "
            "    SUM(CASE WHEN last_seen > datetime('now','-300 seconds') AND is_ps4_capable=1 THEN 1 ELSE 0 END) as ps4_online, "
            "    SUM(CASE WHEN last_seen > datetime('now','-300 seconds') AND is_ps5_capable=1 THEN 1 ELSE 0 END) as ps5_online "
            "  FROM worker_instances GROUP BY worker_key_id"
            ") ic ON ic.worker_key_id = wk.id "
            "GROUP BY wk.id "
            "ORDER BY is_online DESC, tracked DESC, wk.last_used DESC"
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

    # Saves per minute — rolling 10-minute window for stability
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE status = 'done' "
            "  AND created_at > datetime('now', '-10 minutes')"
        )
        done_10m = (await cursor.fetchone())[0]
    finally:
        await db.close()
    stats["spm"] = f"{done_10m / 10:.1f}"

    # Workers Online = sum of online instances per key, falling back to is_online
    # for keys with no instance rows yet (legacy / pre-migration heartbeat).
    workers_online = sum(
        max(w["online_instances"], w["is_online"])
        for w in workers
        if w["is_active"] and not w["suspended_until"]
    )

    return await render_template(
        "admin.html",
        workers=workers,
        jobs=jobs,
        queued=queued,
        total_users=total_users,
        stats=stats,
        op_stats=op_stats,
        workers_online=workers_online,
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

        # Online instances under active, non-suspended keys
        cursor = await db.execute(
            "SELECT COUNT(*) FROM worker_instances wi "
            "JOIN worker_keys wk ON wk.id = wi.worker_key_id "
            "WHERE wi.last_seen > datetime('now', '-300 seconds') "
            "AND wk.is_active = 1 "
            "AND (wk.suspended_until IS NULL OR wk.suspended_until <= datetime('now'))"
        )
        instance_online = (await cursor.fetchone())[0]
        # Plus active keys with a recent heartbeat that have no online instance row yet
        cursor = await db.execute(
            "SELECT COUNT(*) FROM worker_keys wk "
            "WHERE wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-300 seconds') "
            "AND wk.is_active = 1 "
            "AND (wk.suspended_until IS NULL OR wk.suspended_until <= datetime('now')) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM worker_instances wi WHERE wi.worker_key_id = wk.id "
            "  AND wi.last_seen > datetime('now','-300 seconds')"
            ")"
        )
        key_only_online = (await cursor.fetchone())[0]
        workers_online = instance_online + key_only_online

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

    # Saves per minute — rolling 10-minute window
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE status = 'done' "
            "  AND created_at > datetime('now', '-10 minutes')"
        )
        done_10m = (await cursor.fetchone())[0]
    finally:
        await db.close()
    stats["spm"] = f"{done_10m / 10:.1f}"

    return {
        "queued": queued,
        "total_users": total_users,
        "workers_online": workers_online,
        "stats": stats,
        "op_stats": op_stats,
    }


@admin_web_bp.route("/admin/worker-instances/<int:key_id>")
@admin_required
async def worker_instances(key_id):
    """Per-IP instances for a worker key (used to break down the shared global key)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT wi.id, wi.ip, wi.user_agent, wi.first_seen, wi.last_seen, "
            "wi.online_since, wi.last_platform, wi.is_ps4_capable, wi.is_ps5_capable, "
            "wi.jobs_done, wi.jobs_failed, wi.suspended_until, wk.name as key_name, "
            "CASE WHEN wi.last_seen > datetime('now', '-300 seconds') THEN 1 ELSE 0 END as is_online, "
            "CASE WHEN wi.suspended_until IS NOT NULL AND wi.suspended_until > datetime('now') THEN 1 ELSE 0 END as is_suspended, "
            # Avg job duration in seconds over this instance's last 20 done jobs
            # (jobs that have both started_at and completed_at — pre-migration
            # rows are excluded). Proxy for combined network + processing speed.
            "(SELECT AVG(strftime('%s', j.completed_at) - strftime('%s', j.started_at)) "
            " FROM (SELECT completed_at, started_at FROM jobs "
            "       WHERE worker_instance_id = wi.id "
            "         AND status = 'done' "
            "         AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "       ORDER BY completed_at DESC LIMIT 20) AS j) AS avg_duration_s "
            "FROM worker_instances wi "
            "JOIN worker_keys wk ON wk.id = wi.worker_key_id "
            "WHERE wi.worker_key_id = ? "
            "ORDER BY wi.last_seen DESC",
            (key_id,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    for r in rows:
        total = r["jobs_done"] + r["jobs_failed"]
        r["total"] = total
        r["rate"] = f"{100 * r['jobs_done'] // total}%" if total else "-"
    return jsonify({"instances": rows})


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


@admin_web_bp.route("/admin/users")
@admin_required
async def list_users():
    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", "1"))
    per_page = 50

    db = await get_db()
    try:
        if q:
            like = f"%{q}%"
            cursor = await db.execute(
                "SELECT COUNT(*) FROM users WHERE username LIKE ?", (like,)
            )
            total = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT id, username, created_at, banned FROM users "
                "WHERE username LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (like, per_page, (page - 1) * per_page),
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            total = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT id, username, created_at, banned FROM users "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (per_page, (page - 1) * per_page),
            )
        users = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return await render_template(
        "admin_users.html",
        users=users,
        q=q,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@admin_web_bp.route("/admin/ban-user", methods=["POST"])
@admin_required
async def ban_user():
    data = await request.get_json()
    username = (data or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username required"}), 400

    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE users SET banned = 1 WHERE username = ?", (username,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return jsonify({"ok": False, "error": f"User '{username}' not found"}), 404
    finally:
        await db.close()

    return jsonify({"ok": True})


@admin_web_bp.route("/admin/unban-user", methods=["POST"])
@admin_required
async def unban_user():
    data = await request.get_json()
    username = (data or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username required"}), 400

    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE users SET banned = 0 WHERE username = ?", (username,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return jsonify({"ok": False, "error": f"User '{username}' not found"}), 404
    finally:
        await db.close()

    return jsonify({"ok": True})


@admin_web_bp.route("/admin/send-message", methods=["POST"])
@admin_required
async def send_message():
    """Post a banner message to a specific user, or to '*' for broadcast."""
    from quart import session as _session
    data = await request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "JSON body required"}), 400
    target = (data.get("username") or "").strip()
    message = (data.get("message") or "").strip()
    level = (data.get("level") or "info").strip().lower()
    if not target or not message:
        return jsonify({"ok": False, "error": "username and message required"}), 400
    if level not in ("info", "success", "warning", "error"):
        level = "info"
    if len(message) > 2000:
        return jsonify({"ok": False, "error": "message too long (max 2000)"}), 400

    db = await get_db()
    try:
        if target == "*":
            user_id = None
        else:
            cursor = await db.execute(
                "SELECT id FROM users WHERE username = ?", (target,)
            )
            row = await cursor.fetchone()
            if not row:
                return jsonify({"ok": False, "error": f"User '{target}' not found"}), 404
            user_id = row["id"]
        await db.execute(
            "INSERT INTO admin_messages (user_id, sender_id, message, level) VALUES (?, ?, ?, ?)",
            (user_id, _session.get("user_id"), message, level),
        )
        await db.commit()
    finally:
        await db.close()
    return jsonify({"ok": True, "target": target, "level": level})


@admin_web_bp.route("/admin/messages")
@admin_required
async def list_messages():
    """List recently-sent admin messages (for the panel history view)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT m.id, m.message, m.level, m.created_at, m.dismissed_at, "
            "       u.username AS target, su.username AS sender "
            "FROM admin_messages m "
            "LEFT JOIN users u ON u.id = m.user_id "
            "LEFT JOIN users su ON su.id = m.sender_id "
            "ORDER BY m.created_at DESC LIMIT 30"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    return jsonify({"messages": rows})


@admin_web_bp.route("/messages/<int:msg_id>/dismiss", methods=["POST"])
async def dismiss_message(msg_id):
    """Any logged-in user can dismiss a message that's targeted at them or a broadcast."""
    from quart import session as _session
    uid = _session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "auth"}), 401
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE admin_messages SET dismissed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND dismissed_at IS NULL "
            "AND (user_id = ? OR user_id IS NULL)",
            (msg_id, uid),
        )
        await db.commit()
        ok = cursor.rowcount > 0
    finally:
        await db.close()
    return jsonify({"ok": ok})
