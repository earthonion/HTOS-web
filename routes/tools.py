import csv
import io

import httpx
from quart import Blueprint, Response, jsonify, render_template, request, session

from auth import login_required
from models import get_db

VALID_URL_HOSTS = {
    "gs2.ww.prod.dl.playstation.net",
    "sgst.prod.dl.playstation.net",
    "zeus.dl.playstation.net",
    "ares.dl.playstation.net",
    "gs2-ww-prod.psn.akadns.net",
}
from services.filesystem import search_filesystem
from services.functions import search_functions
from services.titles import search_titles

tools_bp = Blueprint("tools", __name__)


@tools_bp.route("/tools")
async def tools_index():
    return await render_template("tools.html")


@tools_bp.route("/tools/title-lookup")
async def title_lookup():
    q = request.args.get("q", "").strip()
    results = []
    if q and len(q) >= 2:
        results = await search_titles(q, limit=50)
    return await render_template("tools_title_lookup.html", q=q, results=results)


@tools_bp.route("/tools/api/title-search")
async def api_title_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = await search_titles(q, limit=20)
    return jsonify({"results": results})


@tools_bp.route("/tools/sfo-viewer")
async def sfo_viewer():
    return await render_template("tools_sfo_viewer.html")


@tools_bp.route("/tools/entitlements")
async def entitlements():
    return await render_template("tools_entitlements.html")


@tools_bp.route("/tools/fs-browser")
async def fs_browser():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    results = []
    if q and len(q) >= 2:
        results = await search_filesystem(q, platform=platform, limit=50)
    return await render_template(
        "tools_fs_browser.html", q=q, platform=platform, results=results
    )


@tools_bp.route("/tools/api/fs-search")
async def api_fs_search():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = await search_filesystem(q, platform=platform, limit=50)
    return jsonify({"results": results})


@tools_bp.route("/tools/function-lookup")
async def function_lookup():
    q = request.args.get("q", "").strip()
    results = []
    if q and len(q) >= 3:
        results = await search_functions(q, limit=50)
    return await render_template("tools_function_lookup.html", q=q, results=results)


@tools_bp.route("/tools/api/function-search")
async def api_function_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 3:
        return jsonify({"results": []})
    results = await search_functions(q, limit=50)
    return jsonify({"results": results})


@tools_bp.route("/tools/syscalls")
async def syscall_lookup():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "ps4")
    if platform not in ("ps4", "ps5"):
        platform = "ps4"
    all_sc = _all_syscalls(platform)
    results = _search_syscalls(q, platform) if q else all_sc
    return await render_template(
        "tools_syscalls.html", q=q, results=results, total=len(all_sc), platform=platform
    )


@tools_bp.route("/tools/api/syscall-search")
async def api_syscall_search():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "ps4")
    if platform not in ("ps4", "ps5"):
        platform = "ps4"
    if not q:
        return jsonify({"results": _all_syscalls(platform)})
    return jsonify({"results": _search_syscalls(q, platform)})


def _load_syscalls(platform):
    import os

    filename = "ps5_syscalls.txt" if platform == "ps5" else "ps4_syscalls.txt"
    path = os.path.join(os.path.dirname(__file__), "..", "data", filename)
    syscalls = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            num, name = line.split("=", 1)
            syscalls.append({"num": int(num.strip()), "name": name.strip().strip('"')})
    return syscalls


_syscalls_cache = {}


def _all_syscalls(platform="ps4"):
    if platform not in _syscalls_cache:
        _syscalls_cache[platform] = _load_syscalls(platform)
    return _syscalls_cache[platform]


def _search_syscalls(q, platform="ps4"):
    q_lower = q.lower()
    results = []
    for s in _all_syscalls(platform):
        if q_lower in s["name"].lower() or q_lower == str(s["num"]):
            results.append(s)
    return results


@tools_bp.route("/tools/api/entitlements", methods=["POST"])
@login_required
async def api_submit_entitlements():
    """Receive extracted entitlements from client and store (dedup by entitlement_id)."""
    data = await request.get_json()
    if not data or "entries" not in data:
        return jsonify({"ok": False, "error": "No entries"}), 400

    entries = data["entries"]
    if not isinstance(entries, list) or len(entries) > 5000:
        return jsonify({"ok": False, "error": "Invalid or too many entries"}), 400

    user_id = session.get("user_id")

    # Filter to valid PSN hostnames only
    from urllib.parse import urlparse

    valid_entries = []
    for e in entries:
        eid = e.get("id", "").strip()
        url = e.get("url", "").strip()
        if not eid or not url:
            continue
        parsed = urlparse(url)
        if parsed.hostname not in VALID_URL_HOSTS:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        valid_entries.append(e)

    if not valid_entries:
        return jsonify({"ok": True, "inserted": 0, "skipped": len(entries)})

    # Spot-check a sample of URLs with HEAD requests to verify they resolve
    import random

    sample = random.sample(valid_entries, min(5, len(valid_entries)))
    verified = 0
    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        for e in sample:
            try:
                resp = await client.head(e["url"].strip())
                if resp.status_code == 200:
                    verified += 1
            except Exception:
                pass

    # If none of the sample URLs resolve, reject the entire batch
    if verified == 0:
        return jsonify({"ok": False, "error": "URL validation failed — none of the sampled URLs resolved."}), 400

    inserted = 0
    db = await get_db()
    try:
        for e in valid_entries:
            eid = e.get("id", "").strip()
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO entitlements "
                    "(entitlement_id, title, title_id, package_url, platform, content_type, contributed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        eid,
                        e.get("title", "")[:200],
                        e.get("title_id", "")[:20],
                        e.get("url", "").strip()[:500],
                        e.get("platform", "")[:10],
                        e.get("content_type", "")[:50],
                        user_id,
                    ),
                )
                inserted += db.total_changes
            except Exception:
                pass
        await db.commit()
    finally:
        await db.close()

    return jsonify({"ok": True, "inserted": inserted})


@tools_bp.route("/tools/entitlements/browse")
@login_required
async def browse_entitlements():
    user_id = session.get("user_id")
    is_admin = session.get("is_admin", False)

    # Non-admins must have contributed at least one entitlement
    if not is_admin:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM entitlements WHERE contributed_by = ?",
                (user_id,),
            )
            count = (await cursor.fetchone())[0]
        finally:
            await db.close()
        if count == 0:
            from quart import flash, redirect, url_for

            await flash(
                "Contribute entitlements using the Entitlement Dumper tool to access the database.",
                "error",
            )
            return redirect(url_for("tools.entitlements"))

    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip().lower()
    if platform not in ("ps4", "ps5"):
        platform = ""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    db = await get_db()
    try:
        where = []
        params = []
        if q:
            like = f"%{q}%"
            where.append("(entitlement_id LIKE ? OR title LIKE ? OR title_id LIKE ?)")
            params.extend([like, like, like])
        if platform:
            where.append("platform = ?")
            params.append(platform)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cursor = await db.execute(
            f"SELECT * FROM entitlements {where_sql} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [per_page + 1, offset],
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        has_next = len(rows) > per_page
        entries = rows[:per_page]

        cursor = await db.execute(
            f"SELECT COUNT(*) FROM entitlements {where_sql}", params
        )
        total = (await cursor.fetchone())[0]
    finally:
        await db.close()

    return await render_template(
        "entitlements_browse.html",
        entries=entries,
        q=q,
        page=page,
        has_next=has_next,
        total=total,
        platform=platform,
        is_admin=is_admin,
    )


@tools_bp.route("/tools/entitlements/csv")
@login_required
async def entitlements_csv():
    user_id = session.get("user_id")
    is_admin = session.get("is_admin", False)

    if not is_admin:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM entitlements WHERE contributed_by = ?",
                (user_id,),
            )
            count = (await cursor.fetchone())[0]
        finally:
            await db.close()
        if count == 0:
            return "Contribute entitlements first to access downloads.", 403

    platform = request.args.get("platform", "").strip().lower()

    db = await get_db()
    try:
        if platform in ("ps4", "ps5"):
            cursor = await db.execute(
                "SELECT entitlement_id, title, title_id, package_url, platform, content_type "
                "FROM entitlements WHERE platform = ? ORDER BY title",
                (platform,),
            )
        else:
            cursor = await db.execute(
                "SELECT entitlement_id, title, title_id, package_url, platform, content_type "
                "FROM entitlements ORDER BY title"
            )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "entitlement_id",
            "title",
            "title_id",
            "package_url",
            "platform",
            "content_type",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["entitlement_id"],
                r["title"],
                r["title_id"],
                r["package_url"],
                r["platform"],
                r["content_type"],
            ]
        )

    filename = f"entitlements_{platform}.csv" if platform else "entitlements_all.csv"
    return Response(
        out.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
