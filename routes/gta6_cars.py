import asyncio
import json
from posixpath import normpath

from quart import Blueprint, abort, current_app, jsonify, request, send_file

from auth import login_required
from services.gta6_cars import (
    APP_ROOT,
    MAX_CONTRIBUTION_BYTES,
    ContributionError,
    accept_contribution,
)


gta6_cars_bp = Blueprint("gta6_cars", __name__)
CAR_ASSET_ROOT = APP_ROOT / "static" / "gta6-cars"
CAR_NATIVE_ROOT = CAR_ASSET_ROOT / "native"
CAR_NATIVE_MODULES = {
    "native-catalog.mjs",
    "native-page.mjs",
    "native-vehicle-mint.mjs",
    "landstalker-xl.mjs",
    "manual-save.mjs",
}


@gta6_cars_bp.before_app_request
async def block_public_native_files():
    if request.endpoint == "static":
        filename = normpath(request.view_args["filename"]).casefold()
        if filename.startswith("gta6-cars/native/"):
            abort(404)


@gta6_cars_bp.route("/gta6/cars", strict_slashes=False)
@login_required
async def gta6_cars():
    return await send_file(CAR_ASSET_ROOT / "index.html", mimetype="text/html", cache_timeout=0)


async def _send_native_asset(path, mimetype):
    response = await send_file(path, mimetype=mimetype, cache_timeout=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@gta6_cars_bp.route("/gta6/cars/native/")
@login_required
async def gta6_cars_native():
    return await _send_native_asset(CAR_NATIVE_ROOT / "index.html", "text/html")


@gta6_cars_bp.route("/gta6/cars/native/<asset>")
@login_required
async def gta6_cars_native_module(asset):
    if asset not in CAR_NATIVE_MODULES:
        abort(404)
    return await _send_native_asset(CAR_NATIVE_ROOT / asset, "text/javascript")


@gta6_cars_bp.route("/gta6/cars/gta6-cars.js")
@login_required
async def gta6_cars_engine():
    return await send_file(CAR_ASSET_ROOT / "gta6-cars.js", mimetype="text/javascript", cache_timeout=3600)


@gta6_cars_bp.route("/gta6/cars/car_library.json")
@login_required
async def gta6_cars_library():
    return await send_file(CAR_ASSET_ROOT / "car_library.json", mimetype="application/json", cache_timeout=0)


@gta6_cars_bp.route("/gta6/cars/THIRD_PARTY_NOTICES.md")
@login_required
async def gta6_cars_notices():
    return await send_file(CAR_ASSET_ROOT / "THIRD_PARTY_NOTICES.md", mimetype="text/markdown", cache_timeout=3600)


@gta6_cars_bp.route("/gta6/cars/contribute", methods=["POST"])
@login_required
async def gta6_cars_contribute():
    if not request.is_json:
        return jsonify({"ok": False, "error": "JSON body required."}), 400
    if request.content_length is not None and request.content_length > MAX_CONTRIBUTION_BYTES:
        return jsonify({"ok": False, "error": "Car file is too large."}), 413
    raw = await request.get_data()
    if len(raw) > MAX_CONTRIBUTION_BYTES:
        return jsonify({"ok": False, "error": "Car file is too large."}), 413
    try:
        data = json.loads(raw)
        contribution_id, added = await asyncio.to_thread(accept_contribution, data)
    except (UnicodeDecodeError, json.JSONDecodeError, ContributionError) as error:
        return jsonify({"ok": False, "error": str(error) or "Invalid car file."}), 400
    except Exception:
        current_app.logger.exception("Could not merge GTA VI car contribution")
        return jsonify({"ok": False, "error": "Could not save these cars."}), 500
    return jsonify({"ok": True, "added": added, "contribution": contribution_id}), 201 if added else 200
