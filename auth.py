import bcrypt
from functools import wraps
from quart import Blueprint, render_template, request, redirect, url_for, session, flash

from models import get_db

auth_bp = Blueprint("auth", __name__)

def login_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return await f(*args, **kwargs)
    return decorated

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

async def is_invite_only():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'invite_only'")
        row = await cursor.fetchone()
        return row and row["value"] == "1"
    finally:
        await db.close()


@auth_bp.route("/register", methods=["GET", "POST"])
async def register():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))

    invite_only = await is_invite_only()

    if request.method == "POST":
        form = await request.form
        username = form.get("username", "").strip()
        password = form.get("password", "")
        confirm = form.get("confirm", "")
        invite_code = form.get("invite_code", "").strip()

        if not username or not password:
            await flash("Username and password are required.", "error")
            return await render_template("register.html", invite_only=invite_only)

        if len(username) < 3 or len(username) > 30:
            await flash("Username must be 3-30 characters.", "error")
            return await render_template("register.html", invite_only=invite_only)

        if len(password) < 6:
            await flash("Password must be at least 6 characters.", "error")
            return await render_template("register.html", invite_only=invite_only)

        if password != confirm:
            await flash("Passwords do not match.", "error")
            return await render_template("register.html", invite_only=invite_only)

        if invite_only and not invite_code:
            await flash("An invite code is required to register.", "error")
            return await render_template("register.html", invite_only=invite_only)

        db = await get_db()
        try:
            # Validate invite code if invite-only
            if invite_only:
                cursor = await db.execute(
                    "SELECT id FROM invite_codes WHERE code = ? AND used_by IS NULL",
                    (invite_code,)
                )
                code_row = await cursor.fetchone()
                if not code_row:
                    await flash("Invalid or already used invite code.", "error")
                    return await render_template("register.html", invite_only=invite_only)

            existing = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
            if await existing.fetchone():
                await flash("Username already taken.", "error")
                return await render_template("register.html", invite_only=invite_only)

            pw_hash = hash_password(password)
            cursor = await db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, pw_hash)
            )
            user_id = cursor.lastrowid

            # Mark invite code as used
            if invite_only:
                await db.execute(
                    "UPDATE invite_codes SET used_by = ?, used_at = CURRENT_TIMESTAMP WHERE code = ?",
                    (user_id, invite_code)
                )

            await db.commit()
            session["user_id"] = user_id
            session["username"] = username
            return redirect(url_for("main.dashboard"))
        finally:
            await db.close()

    return await render_template("register.html", invite_only=invite_only)

@auth_bp.route("/login", methods=["GET", "POST"])
async def login():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        form = await request.form
        username = form.get("username", "").strip()
        password = form.get("password", "")

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id, password_hash FROM users WHERE username = ?", (username,)
            )
            row = await cursor.fetchone()
            if not row or not check_password(password, row["password_hash"]):
                await flash("Invalid username or password.", "error")
                return await render_template("login.html")

            session["user_id"] = row["id"]
            session["username"] = username
            return redirect(url_for("main.dashboard"))
        finally:
            await db.close()

    return await render_template("login.html")

@auth_bp.route("/logout", methods=["POST"])
async def logout():
    session.clear()
    return redirect(url_for("auth.login"))
