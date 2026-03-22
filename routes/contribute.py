import hashlib
import hmac

from quart import Blueprint, render_template, request, redirect, url_for, session, flash

from auth import login_required
from config import WORKER_SIGNING_KEY
from models import get_db

contribute_bp = Blueprint("contribute", __name__)


def generate_worker_key(user_id: int, key_id: int) -> str:
    """Generate HMAC-SHA256 worker key from user_id and key_id."""
    message = f"{user_id}:{key_id}".encode()
    return hmac.new(WORKER_SIGNING_KEY.encode(), message, hashlib.sha256).hexdigest()


@contribute_bp.route("/contribute", methods=["GET", "POST"])
@login_required
async def contribute():
    user_id = session["user_id"]

    if request.method == "POST":
        form = await request.form
        name = form.get("name", "").strip()

        if not name:
            await flash("Please provide a name for this key.", "error")
            return redirect(url_for("contribute.contribute"))

        if len(name) > 64:
            await flash("Key name must be 64 characters or less.", "error")
            return redirect(url_for("contribute.contribute"))

        db = await get_db()
        try:
            # Insert a placeholder row to get the key_id
            cursor = await db.execute(
                "INSERT INTO worker_keys (user_id, key, name) VALUES (?, '', ?)",
                (user_id, name)
            )
            key_id = cursor.lastrowid

            # Generate the HMAC key using the row ID
            key = generate_worker_key(user_id, key_id)

            # Update the row with the real key
            await db.execute(
                "UPDATE worker_keys SET key = ? WHERE id = ?",
                (key, key_id)
            )
            await db.commit()
        finally:
            await db.close()

        # Store in session so it's not exposed in URL
        session["new_worker_key"] = key
        await flash("Worker key created! Copy it now, it won't be shown again.", "success")
        return redirect(url_for("contribute.contribute"))

    # GET — list user's keys with success rate from job_stats
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT wk.id, wk.name, wk.is_active, wk.created_at, wk.last_used, wk.last_platform, "
            "wk.jobs_completed, wk.suspended_until, wk.online_since, "
            "CASE WHEN wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-300 seconds') THEN 1 ELSE 0 END as is_online, "
            "CASE WHEN wk.suspended_until IS NOT NULL AND wk.suspended_until > datetime('now') THEN 1 ELSE 0 END as is_suspended, "
            "COALESCE(ps.hist_done, 0) as stats_done, "
            "COALESCE(ps.hist_failed, 0) as stats_failed, "
            "COALESCE(ps.hist_total, 0) as stats_total "
            "FROM worker_keys wk "
            "LEFT JOIN ("
            "  SELECT worker_key_id, SUM(done) as hist_done, SUM(failed) as hist_failed, "
            "  SUM(total) as hist_total FROM job_stats GROUP BY worker_key_id"
            ") ps ON ps.worker_key_id = wk.id "
            "WHERE wk.user_id = ? ORDER BY wk.created_at DESC",
            (user_id,)
        )
        keys = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

    new_key = session.pop("new_worker_key", None)

    if any(k["is_suspended"] for k in keys):
        await flash(
            'Oops! Your worker has been suspended! This isn\'t your fault. Your console just needs to be rebooted '
            'and make sure you are running the <a href="https://github.com/earthonion/garlicsaves-worker/releases" target="_blank">latest worker ELF</a>, '
            'then simply click the Reactivate button. Suspension happens after 10 failed jobs in a row.',
            "error"
        )

    return await render_template("contribute.html", keys=keys, new_key=new_key)


@contribute_bp.route("/contribute/<int:key_id>/revoke", methods=["POST"])
@login_required
async def revoke_key(key_id):
    user_id = session["user_id"]
    db = await get_db()
    try:
        await db.execute(
            "UPDATE worker_keys SET is_active = 0 WHERE id = ? AND user_id = ?",
            (key_id, user_id)
        )
        await db.commit()
    finally:
        await db.close()

    await flash("Worker key revoked.", "success")
    return redirect(url_for("contribute.contribute"))


@contribute_bp.route("/contribute/<int:key_id>/reactivate", methods=["POST"])
@login_required
async def reactivate_key(key_id):
    user_id = session["user_id"]
    db = await get_db()
    try:
        await db.execute(
            "UPDATE worker_keys SET suspended_until = NULL WHERE id = ? AND user_id = ?",
            (key_id, user_id)
        )
        await db.commit()
    finally:
        await db.close()

    await flash("Worker reactivated. Make sure you've rebooted your console!", "success")
    return redirect(url_for("contribute.contribute"))
