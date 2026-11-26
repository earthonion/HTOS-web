import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from quart import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from auth import _real_client_ip, login_required
from models import get_db


device_bp = Blueprint("device", __name__)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LIFETIME = timedelta(minutes=10)
MAX_CODES_PER_IP_PER_HOUR = 20


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_code(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_user_code(value):
    return "".join(c for c in value.upper() if c in CODE_ALPHABET)


def _new_user_code():
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def _verification_uri(user_code):
    base = os.getenv("PUBLIC_BASE_URL") or request.host_url
    return f"{base.rstrip('/')}{url_for('device.authorize_device')}?code={user_code}"


@device_bp.route("/api/v1/device/code", methods=["POST"])
async def create_device_code():
    body = await request.get_json(silent=True) or {}
    name = " ".join(str(body.get("name") or "GarlicTransfer installation").split())
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        return jsonify({"error": "Installation name must be 1-64 printable characters"}), 400
    client_ip = _real_client_ip()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM device_authorizations "
            "WHERE creator_ip = ? AND created_at > datetime('now', '-1 hour')",
            (client_ip,),
        )
        if (await cursor.fetchone())[0] >= MAX_CODES_PER_IP_PER_HOUR:
            return jsonify({"error": "Too many authorization attempts; try again later"}), 429

        device_code = secrets.token_urlsafe(32)
        user_code = _new_user_code()
        await db.execute(
            "INSERT INTO device_authorizations "
            "(device_code_hash, user_code, device_name, creator_ip, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_code(device_code), _normalize_user_code(user_code), name, client_ip, _utcnow() + CODE_LIFETIME),
        )
        await db.commit()
    finally:
        await db.close()
    return jsonify({
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": _verification_uri(user_code),
        "expires_in": int(CODE_LIFETIME.total_seconds()),
        "interval": 3,
    }), 201


@device_bp.route("/api/v1/device/token", methods=["POST"])
async def poll_device_token():
    body = await request.get_json(silent=True) or {}
    device_code = str(body.get("device_code") or "")
    if len(device_code) < 32 or len(device_code) > 128:
        return jsonify({"error": "Invalid device code"}), 400
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM device_authorizations WHERE device_code_hash = ?",
            (_hash_code(device_code),),
        )
        row = await cursor.fetchone()
        if not row:
            return jsonify({"error": "Invalid device code"}), 404
        if row["status"] == "pending" and row["expires_at"] <= _utcnow().isoformat(sep=" "):
            await db.execute(
                "UPDATE device_authorizations SET status = 'expired' WHERE device_code_hash = ?",
                (row["device_code_hash"],),
            )
            await db.commit()
            return jsonify({"error": "Authorization expired"}), 410
        if row["status"] == "pending":
            now = _utcnow()
            if row["last_poll_at"]:
                last_poll = datetime.fromisoformat(str(row["last_poll_at"]))
                if (now - last_poll).total_seconds() < 3:
                    return jsonify({"error": "Polling too quickly"}), 429
            await db.execute(
                "UPDATE device_authorizations SET last_poll_at = ? WHERE device_code_hash = ?",
                (now, row["device_code_hash"]),
            )
            await db.commit()
            return jsonify({"status": "authorization_pending"}), 202
        if row["status"] == "denied":
            return jsonify({"error": "Authorization was denied"}), 403
        if row["status"] != "approved":
            return jsonify({"error": "Authorization expired or was already used"}), 410

        cursor = await db.execute(
            "SELECT worker_keys.key, worker_keys.installation_id, users.username "
            "FROM worker_keys JOIN users ON users.id = worker_keys.user_id "
            "WHERE worker_keys.id = ? AND worker_keys.user_id = ? AND worker_keys.is_active = 1",
            (row["worker_key_id"], row["user_id"]),
        )
        credential = await cursor.fetchone()
        if not credential:
            return jsonify({"error": "Authorized installation is no longer active"}), 410
        cursor = await db.execute(
            "UPDATE device_authorizations SET status = 'consumed' "
            "WHERE device_code_hash = ? AND status = 'approved'",
            (row["device_code_hash"],),
        )
        if cursor.rowcount != 1:
            return jsonify({"error": "Authorization was already used"}), 410
        await db.commit()
        return jsonify({
            "status": "authorized",
            "worker_key": credential["key"],
            "installation_id": credential["installation_id"],
            "username": credential["username"],
        })
    finally:
        await db.close()


@device_bp.route("/device", methods=["GET", "POST"])
@login_required
async def authorize_device():
    if request.method == "GET":
        return await render_template("device.html", code=request.args.get("code", ""))

    form = await request.form
    user_code = _normalize_user_code(form.get("code", ""))
    action = form.get("action", "approve")
    if len(user_code) != 8:
        await flash("Enter the eight-character code shown in GarlicTransfer.", "error")
        return redirect(url_for("device.authorize_device"))

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM device_authorizations WHERE user_code IN (?, ?)",
            (user_code, f"{user_code[:4]}-{user_code[4:]}"),
        )
        row = await cursor.fetchone()
        if not row or row["status"] != "pending" or row["expires_at"] <= _utcnow().isoformat(sep=" "):
            await flash("That authorization code is invalid or expired.", "error")
            return redirect(url_for("device.authorize_device"))
        if action == "deny":
            await db.execute(
                "UPDATE device_authorizations SET status = 'denied', user_id = ? "
                "WHERE device_code_hash = ?",
                (session["user_id"], row["device_code_hash"]),
            )
            await db.commit()
            await flash("Installation authorization denied.", "success")
            return redirect(url_for("main.dashboard"))

        installation_id = str(uuid.uuid4())
        worker_key = secrets.token_hex(32)
        cursor = await db.execute(
            "INSERT INTO worker_keys "
            "(user_id, key, name, is_active, api_enabled, ppr_derive_enabled, "
            " job_rate_limit, installation_id) "
            "VALUES (?, ?, ?, 1, 1, 1, 30, ?)",
            (session["user_id"], worker_key, row["device_name"], installation_id),
        )
        cursor = await db.execute(
            "UPDATE device_authorizations SET status = 'approved', user_id = ?, worker_key_id = ? "
            "WHERE device_code_hash = ? AND status = 'pending'",
            (session["user_id"], cursor.lastrowid, row["device_code_hash"]),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            await flash("That authorization code was already used.", "error")
            return redirect(url_for("device.authorize_device"))
        await db.commit()
    finally:
        await db.close()

    await flash("GarlicTransfer installation authorized. Return to the app to finish.", "success")
    return redirect(url_for("main.dashboard"))
