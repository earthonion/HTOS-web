from quart import Blueprint, render_template, request, jsonify

from services.titles import search_titles
from services.filesystem import search_filesystem
from services.functions import search_functions

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
    return await render_template("tools_fs_browser.html", q=q, platform=platform, results=results)


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
