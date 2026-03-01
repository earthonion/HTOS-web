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

    # GET — list user's keys
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, is_active, created_at, last_used, last_platform, "
            "CASE WHEN last_used IS NOT NULL AND last_used > datetime('now', '-90 seconds') THEN 1 ELSE 0 END as is_online "
            "FROM worker_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        )
        keys = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

    new_key = session.pop("new_worker_key", None)
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
