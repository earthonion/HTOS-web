import csv
import io

from quart import Blueprint, Response, jsonify, render_template, request, session

from auth import admin_required, login_required
from models import get_db
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
        results = search_titles(q, limit=50)
    return await render_template("tools_title_lookup.html", q=q, results=results)


@tools_bp.route("/tools/api/title-search")
async def api_title_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = search_titles(q, limit=20)
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
        results = search_filesystem(q, platform=platform, limit=50)
    return await render_template(
        "tools_fs_browser.html", q=q, platform=platform, results=results
    )


@tools_bp.route("/tools/api/fs-search")
async def api_fs_search():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = search_filesystem(q, platform=platform, limit=50)
    return jsonify({"results": results})


@tools_bp.route("/tools/function-lookup")
async def function_lookup():
    q = request.args.get("q", "").strip()
    results = []
    if q and len(q) >= 3:
        results = search_functions(q, limit=50)
    return await render_template("tools_function_lookup.html", q=q, results=results)


@tools_bp.route("/tools/api/function-search")
async def api_function_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 3:
        return jsonify({"results": []})
    results = search_functions(q, limit=50)
    return jsonify({"results": results})


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
    inserted = 0
    db = await get_db()
    try:
        for e in entries:
            eid = e.get("id", "").strip()
            url = e.get("url", "").strip()
            if not eid or not url:
                continue
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO entitlements "
                    "(entitlement_id, title, title_id, package_url, platform, content_type, contributed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        eid,
                        e.get("title", "")[:200],
                        e.get("title_id", "")[:20],
                        e.get("url", "")[:500],
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


@tools_bp.route("/admin/entitlements")
@admin_required
async def admin_entitlements():
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
        "admin_entitlements.html",
        entries=entries,
        q=q,
        page=page,
        has_next=has_next,
        total=total,
        platform=platform,
    )


@tools_bp.route("/admin/entitlements/csv")
@admin_required
async def admin_entitlements_csv():
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
