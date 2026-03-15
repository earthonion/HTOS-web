#!/usr/bin/env python3
"""HTOS Admin CLI — manage users, worker keys, and jobs from the server."""

import argparse
import asyncio
import secrets
import sys

import bcrypt
import aiosqlite

from config import DATABASE_PATH


async def get_db():
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    return db


# ── Users ──

async def list_users():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id, username, created_at FROM users ORDER BY id")
        rows = await cursor.fetchall()
        if not rows:
            print("No users.")
            return
        print(f"{'ID':<6}{'Username':<20}{'Created'}")
        print("-" * 50)
        for r in rows:
            print(f"{r['id']:<6}{r['username']:<20}{r['created_at']}")
    finally:
        await db.close()


async def change_password(username, new_password):
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (pw_hash, username)
        )
        await db.commit()
        if cursor.rowcount == 0:
            print(f"User '{username}' not found.")
        else:
            print(f"Password updated for '{username}'.")
    finally:
        await db.close()


async def delete_user(username):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        if not row:
            print(f"User '{username}' not found.")
            return
        user_id = row["id"]
        await db.execute("DELETE FROM worker_keys WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        print(f"User '{username}' and all associated data deleted.")
    finally:
        await db.close()


# ── Worker Keys ──

async def list_keys(username=None):
    db = await get_db()
    try:
        query = (
            "SELECT wk.id, u.username, wk.name, wk.is_active, wk.created_at, wk.last_used, "
            "wk.last_platform, "
            "CASE WHEN wk.is_active = 1 AND wk.last_used IS NOT NULL "
            "AND wk.last_used > datetime('now', '-90 seconds') THEN 1 ELSE 0 END as is_online "
            "FROM worker_keys wk JOIN users u ON wk.user_id = u.id "
        )
        if username:
            query += "WHERE u.username = ? ORDER BY wk.id"
            cursor = await db.execute(query, (username,))
        else:
            query += "ORDER BY wk.id"
            cursor = await db.execute(query)
        rows = await cursor.fetchall()
        if not rows:
            print("No worker keys.")
            return
        print(f"{'ID':<6}{'User':<16}{'Name':<20}{'Active':<8}{'Status':<12}{'Created':<22}{'Last Used'}")
        print("-" * 100)
        for r in rows:
            active = "yes" if r["is_active"] else "no"
            last = r["last_used"] or "never"
            if r["is_online"]:
                status = f"online/{r['last_platform'] or 'ps4'}"
            else:
                status = "offline"
            print(f"{r['id']:<6}{r['username']:<16}{r['name']:<20}{active:<8}{status:<12}{r['created_at']:<22}{last}")
    finally:
        await db.close()


async def revoke_key(key_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE worker_keys SET is_active = 0 WHERE id = ?", (key_id,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            print(f"Key ID {key_id} not found.")
        else:
            print(f"Key ID {key_id} revoked.")
    finally:
        await db.close()


async def revoke_all_keys(username):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        if not row:
            print(f"User '{username}' not found.")
            return
        cursor = await db.execute(
            "UPDATE worker_keys SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (row["id"],)
        )
        await db.commit()
        print(f"Revoked {cursor.rowcount} key(s) for '{username}'.")
    finally:
        await db.close()


async def delete_key(key_id):
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM worker_keys WHERE id = ?", (key_id,))
        await db.commit()
        if cursor.rowcount == 0:
            print(f"Key ID {key_id} not found.")
        else:
            print(f"Key ID {key_id} deleted.")
    finally:
        await db.close()


# ── Invites ──

async def invite_status():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'invite_only'")
        row = await cursor.fetchone()
        enabled = row and row["value"] == "1"
        print(f"Invite-only registration: {'ENABLED' if enabled else 'DISABLED'}")
    finally:
        await db.close()


async def invite_toggle(on: bool):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('invite_only', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if on else "0",)
        )
        await db.commit()
        print(f"Invite-only registration {'ENABLED' if on else 'DISABLED'}.")
    finally:
        await db.close()


async def invite_create(count=1):
    db = await get_db()
    try:
        codes = []
        for _ in range(count):
            code = secrets.token_hex(8)
            await db.execute(
                "INSERT INTO invite_codes (code) VALUES (?)", (code,)
            )
            codes.append(code)
        await db.commit()
        print(f"Created {count} invite code(s):")
        for c in codes:
            print(f"  {c}")
    finally:
        await db.close()


async def invite_list():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT ic.id, ic.code, ic.created_at, ic.used_at, u.username as used_by_name "
            "FROM invite_codes ic LEFT JOIN users u ON ic.used_by = u.id "
            "ORDER BY ic.id"
        )
        rows = await cursor.fetchall()
        if not rows:
            print("No invite codes.")
            return
        print(f"{'ID':<6}{'Code':<20}{'Status':<14}{'Created':<22}{'Used By'}")
        print("-" * 80)
        for r in rows:
            status = f"used {r['used_at'][:10]}" if r["used_at"] else "available"
            used_by = r["used_by_name"] or ""
            print(f"{r['id']:<6}{r['code']:<20}{status:<14}{r['created_at']:<22}{used_by}")
    finally:
        await db.close()


async def invite_delete(code_id):
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM invite_codes WHERE id = ?", (code_id,))
        await db.commit()
        if cursor.rowcount == 0:
            print(f"Invite ID {code_id} not found.")
        else:
            print(f"Invite ID {code_id} deleted.")
    finally:
        await db.close()


# ── Jobs ──

async def list_jobs(limit=20):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT j.id, u.username, j.operation, j.status, j.created_at, j.error, "
            "wk.name as worker_name "
            "FROM jobs j JOIN users u ON j.user_id = u.id "
            "LEFT JOIN worker_keys wk ON j.worker_key_id = wk.id "
            "ORDER BY j.created_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        if not rows:
            print("No jobs.")
            return
        print(f"{'ID':<38}{'User':<16}{'Op':<12}{'Status':<10}{'Worker':<16}{'Created'}")
        print("-" * 116)
        for r in rows:
            worker = r["worker_name"] or "-"
            print(f"{r['id']:<38}{r['username']:<16}{r['operation']:<12}{r['status']:<10}{worker:<16}{r['created_at']}")
            if r["error"]:
                print(f"  error: {r['error']}")
    finally:
        await db.close()


async def worker_stats():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT wk.id, wk.name, wk.last_platform, wk.is_active, wk.jobs_completed, "
            "COALESCE(SUM(CASE WHEN j.status = 'done' THEN 1 ELSE 0 END), 0) as done, "
            "COALESCE(SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END), 0) as failed, "
            "COUNT(j.id) as tracked, "
            "CASE WHEN wk.last_used IS NOT NULL AND wk.last_used > datetime('now', '-90 seconds') "
            "THEN 'ONLINE' ELSE 'offline' END as status "
            "FROM worker_keys wk "
            "LEFT JOIN jobs j ON j.worker_key_id = wk.id "
            "GROUP BY wk.id "
            "ORDER BY tracked DESC, wk.last_used DESC"
        )
        rows = await cursor.fetchall()
        if not rows:
            print("No worker keys.")
            return
        print(f"{'ID':<6}{'Name':<22}{'Plat':<6}{'Status':<10}{'Done':<6}{'Fail':<6}{'Total':<7}{'Rate':<8}{'Active'}")
        print("-" * 80)
        for r in rows:
            total = r["done"] + r["failed"]
            rate = f"{100 * r['done'] // total}%" if total > 0 else "-"
            active = "yes" if r["is_active"] else "REVOKED"
            print(f"{r['id']:<6}{r['name']:<22}{r['last_platform']:<6}{r['status']:<10}"
                  f"{r['done']:<6}{r['failed']:<6}{total:<7}{rate:<8}{active}")
    finally:
        await db.close()


async def clear_jobs(status=None):
    db = await get_db()
    try:
        # Snapshot stats before deleting
        where = "WHERE status = ?" if status else ""
        params = (status,) if status else ()
        await db.execute(
            f"INSERT INTO job_stats (operation, worker_key_id, done, failed, total) "
            f"SELECT operation, COALESCE(worker_key_id, 0), "
            f"SUM(CASE WHEN status='done' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
            f"COUNT(*) "
            f"FROM jobs {where} "
            f"GROUP BY operation, COALESCE(worker_key_id, 0) "
            f"ON CONFLICT(operation, worker_key_id) DO UPDATE SET "
            f"done = job_stats.done + excluded.done, "
            f"failed = job_stats.failed + excluded.failed, "
            f"total = job_stats.total + excluded.total",
            params
        )
        if status:
            cursor = await db.execute("DELETE FROM jobs WHERE status = ?", (status,))
        else:
            cursor = await db.execute("DELETE FROM jobs")
        await db.commit()
        label = f"with status '{status}'" if status else ""
        print(f"Deleted {cursor.rowcount} job(s) {label}. Stats preserved.")
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="HTOS Admin CLI")
    sub = parser.add_subparsers(dest="command")

    # Users
    sub.add_parser("users", help="List all users")

    p = sub.add_parser("passwd", help="Change a user's password")
    p.add_argument("username")
    p.add_argument("password")

    p = sub.add_parser("deluser", help="Delete a user and all their data")
    p.add_argument("username")

    # Keys
    p = sub.add_parser("keys", help="List worker keys")
    p.add_argument("--user", default=None, help="Filter by username")

    p = sub.add_parser("revoke", help="Revoke a worker key by ID")
    p.add_argument("key_id", type=int)

    p = sub.add_parser("revoke-all", help="Revoke all keys for a user")
    p.add_argument("username")

    p = sub.add_parser("delkey", help="Delete a worker key by ID")
    p.add_argument("key_id", type=int)

    # Invites
    sub.add_parser("invite-status", help="Show invite-only status")
    sub.add_parser("invite-on", help="Enable invite-only registration")
    sub.add_parser("invite-off", help="Disable invite-only registration")

    p = sub.add_parser("invite-create", help="Generate invite codes")
    p.add_argument("--count", type=int, default=1, help="Number of codes to generate")

    sub.add_parser("invite-list", help="List all invite codes")

    p = sub.add_parser("invite-delete", help="Delete an invite code by ID")
    p.add_argument("code_id", type=int)

    # Jobs
    p = sub.add_parser("jobs", help="List recent jobs")
    p.add_argument("--limit", type=int, default=20)

    sub.add_parser("worker-stats", help="Show worker success rates")

    p = sub.add_parser("clear-jobs", help="Delete jobs")
    p.add_argument("--status", default=None, help="Only delete jobs with this status")

    args = parser.parse_args()

    commands = {
        "users": lambda: list_users(),
        "passwd": lambda: change_password(args.username, args.password),
        "deluser": lambda: delete_user(args.username),
        "keys": lambda: list_keys(args.user),
        "revoke": lambda: revoke_key(args.key_id),
        "revoke-all": lambda: revoke_all_keys(args.username),
        "delkey": lambda: delete_key(args.key_id),
        "invite-status": lambda: invite_status(),
        "invite-on": lambda: invite_toggle(True),
        "invite-off": lambda: invite_toggle(False),
        "invite-create": lambda: invite_create(args.count),
        "invite-list": lambda: invite_list(),
        "invite-delete": lambda: invite_delete(args.code_id),
        "jobs": lambda: list_jobs(args.limit),
        "worker-stats": lambda: worker_stats(),
        "clear-jobs": lambda: clear_jobs(args.status),
    }

    if args.command is None or args.command == "help":
        print("""HTOS Admin CLI

  Users:
    users                         List all users
    passwd <username> <password>  Change a user's password
    deluser <username>            Delete user and all their data

  Worker Keys:
    keys [--user <username>]      List worker keys (optionally filter by user)
    revoke <key_id>               Revoke a worker key by ID
    revoke-all <username>         Revoke all keys for a user
    delkey <key_id>               Permanently delete a worker key

  Invites:
    invite-status                 Show if invite-only is enabled
    invite-on                     Enable invite-only registration
    invite-off                    Disable invite-only registration (default)
    invite-create [--count N]     Generate invite codes (default 1)
    invite-list                   List all invite codes and usage
    invite-delete <code_id>       Delete an invite code by ID

  Jobs:
    jobs [--limit N]              List recent jobs (default 20)
    worker-stats                  Show worker success rates
    clear-jobs [--status STATUS]  Delete jobs (optionally filter by status)

  Examples:
    python admin.py users
    python admin.py passwd ryan newpass123
    python admin.py keys --user ryan
    python admin.py revoke 3
    python admin.py invite-on
    python admin.py invite-create --count 5
    python admin.py invite-list
    python admin.py jobs --limit 50
    python admin.py clear-jobs --status failed""")
        sys.exit(0)

    asyncio.run(commands[args.command]())


if __name__ == "__main__":
    main()
