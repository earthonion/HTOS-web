from quart import Blueprint, render_template, session, redirect, url_for

from auth import login_required
from models import get_db
from services.jobs import get_user_jobs

main_bp = Blueprint("main", __name__)

@main_bp.route("/")
async def index():
    if session.get("user_id"):
        return redirect(url_for("main.dashboard"))
    # Landing page stats
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM worker_keys WHERE is_active = 1 "
            "AND last_used IS NOT NULL AND last_used > datetime('now', '-300 seconds')"
        )
        total_workers = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COALESCE(SUM(jobs_completed), 0) FROM worker_keys")
        total_jobs = (await cursor.fetchone())[0]
    finally:
        await db.close()
    return await render_template("landing.html", total_workers=total_workers, total_jobs=total_jobs)

@main_bp.route("/dashboard")
@login_required
async def dashboard():
    user_id = session["user_id"]
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, account_id FROM profiles WHERE user_id = ?", (user_id,)
        )
        profiles = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    jobs = await get_user_jobs(user_id)

    return await render_template("dashboard.html",
        profiles=profiles,
        jobs=jobs,
        username=session.get("username", "")
    )

@main_bp.route("/about")
async def about():
    return await render_template("about.html")

@main_bp.route("/profiles", methods=["POST"])
@login_required
async def create_profile():
    from quart import request, flash
    from utils.orbis import checkid

    form = await request.form
    name = form.get("name", "").strip()
    account_id = form.get("account_id", "").strip().lower()

    if not name or not account_id:
        await flash("Name and account ID are required.", "error")
        return redirect(url_for("main.dashboard"))

    if len(name) > 20:
        await flash("Name must be 20 characters or less.", "error")
        return redirect(url_for("main.dashboard"))

    if not checkid(account_id):
        await flash("Invalid account ID. Must be 16 hex characters.", "error")
        return redirect(url_for("main.dashboard"))

    # User enters account ID from USB folder (little-endian).
    # Swap to big-endian for storage. Worker will swap back before patching SFO.
    account_id = "".join(reversed([account_id[i:i+2] for i in range(0, 16, 2)]))

    user_id = session["user_id"]
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO profiles (user_id, name, account_id) VALUES (?, ?, ?)",
            (user_id, name, account_id)
        )
        await db.commit()
    finally:
        await db.close()

    await flash(f"Profile '{name}' created.", "success")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/profiles/<int:profile_id>/swap-endian", methods=["POST"])
@login_required
async def swap_endian(profile_id):
    user_id = session["user_id"]
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT account_id FROM profiles WHERE id = ? AND user_id = ?",
            (profile_id, user_id)
        )
        profile = await cursor.fetchone()
        if profile:
            aid = profile["account_id"]
            swapped = "".join(reversed([aid[i:i+2] for i in range(0, len(aid), 2)]))
            await db.execute(
                "UPDATE profiles SET account_id = ? WHERE id = ? AND user_id = ?",
                (swapped, profile_id, user_id)
            )
            await db.commit()
    finally:
        await db.close()

    return redirect(url_for("main.dashboard"))


@main_bp.route("/profiles/<int:profile_id>/delete", methods=["POST"])
@login_required
async def delete_profile(profile_id):
    user_id = session["user_id"]
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM profiles WHERE id = ? AND user_id = ?", (profile_id, user_id)
        )
        await db.commit()
    finally:
        await db.close()

    return redirect(url_for("main.dashboard"))
