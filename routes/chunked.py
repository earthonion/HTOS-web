import os
import shutil
import time
import uuid

from quart import Blueprint, abort, request, session

from config import CHUNK_DIR, CHUNK_EXPIRY

chunked_bp = Blueprint("chunked", __name__, url_prefix="/api/upload")


def _require_auth(f):
    """Allow either session auth (web UI) or worker key auth."""
    from functools import wraps

    @wraps(f)
    async def decorated(*args, **kwargs):
        # Session auth (logged-in web user)
        if session.get("user_id"):
            return await f(*args, **kwargs)
        # Worker key auth
        key = request.headers.get("X-Worker-Key", "")
        if key:
            import hmac

            from config import WORKER_KEY
            from routes.api import validate_worker_key

            if (
                WORKER_KEY and hmac.compare_digest(key, WORKER_KEY)
            ) or await validate_worker_key(key):
                return await f(*args, **kwargs)
        abort(401)

    return decorated


@chunked_bp.route("/init", methods=["POST"])
@_require_auth
async def init_upload():
    """Start a chunked upload session."""
    data = await request.get_json()
    if not data or "filename" not in data or "total_size" not in data:
        abort(400)

    upload_id = str(uuid.uuid4())
    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    os.makedirs(chunk_dir, exist_ok=True)

    # Write metadata (sanitize filename)
    import json

    safe_name = os.path.basename(data["filename"])
    if not safe_name:
        abort(400)
    meta = {
        "filename": safe_name,
        "total_size": data["total_size"],
        "chunk_size": data.get("chunk_size", 50 * 1024 * 1024),
        "created_at": time.time(),
    }
    with open(os.path.join(chunk_dir, "meta.json"), "w") as f:
        json.dump(meta, f)

    return {"upload_id": upload_id}


@chunked_bp.route("/<upload_id>/chunk/<int:index>", methods=["POST"])
@_require_auth
async def upload_chunk(upload_id, index):
    """Upload one chunk."""
    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    if not os.path.isdir(chunk_dir):
        abort(404)

    body = await request.get_data()
    if not body:
        abort(400)

    chunk_path = os.path.join(chunk_dir, f"{index}.part")
    with open(chunk_path, "wb") as f:
        f.write(body)

    return {"ok": True, "index": index}


@chunked_bp.route("/<upload_id>/complete", methods=["POST"])
@_require_auth
async def complete_upload(upload_id):
    """Assemble chunks into final file. Returns path to assembled file."""
    chunk_dir = os.path.join(CHUNK_DIR, upload_id)
    if not os.path.isdir(chunk_dir):
        abort(404)

    import json

    meta_path = os.path.join(chunk_dir, "meta.json")
    if not os.path.isfile(meta_path):
        abort(404)
    with open(meta_path) as f:
        meta = json.load(f)

    # Find and sort chunk parts
    parts = sorted(
        [f for f in os.listdir(chunk_dir) if f.endswith(".part")],
        key=lambda x: int(x.replace(".part", "")),
    )
    if not parts:
        abort(400)

    # Assemble into final file (sanitize filename to prevent traversal)
    filename = os.path.basename(meta["filename"])
    if not filename:
        abort(400)
    final_path = os.path.join(chunk_dir, filename)
    with open(final_path, "wb") as out:
        for part in parts:
            part_path = os.path.join(chunk_dir, part)
            with open(part_path, "rb") as inp:
                shutil.copyfileobj(inp, out)

    # Clean up part files
    for part in parts:
        os.remove(os.path.join(chunk_dir, part))

    return {"ok": True, "upload_id": upload_id, "path": final_path}


def cleanup_expired_chunks():
    """Remove chunk directories older than CHUNK_EXPIRY seconds."""
    import json

    now = time.time()
    if not os.path.isdir(CHUNK_DIR):
        return
    for name in os.listdir(CHUNK_DIR):
        chunk_dir = os.path.join(CHUNK_DIR, name)
        if not os.path.isdir(chunk_dir):
            continue
        meta_path = os.path.join(chunk_dir, "meta.json")
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if now - meta.get("created_at", 0) > CHUNK_EXPIRY:
                shutil.rmtree(chunk_dir, ignore_errors=True)
        except (OSError, json.JSONDecodeError, KeyError):
            # If meta is missing/corrupt and dir is old, clean up
            try:
                stat = os.stat(chunk_dir)
                if now - stat.st_mtime > CHUNK_EXPIRY:
                    shutil.rmtree(chunk_dir, ignore_errors=True)
            except OSError:
                pass
