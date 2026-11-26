import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from quart import Blueprint, abort, request


hooks_bp = Blueprint("hooks", __name__)
SYNC_TRIGGER = Path("/opt/htos/workspace/gta6-editor-upstream/webhook.trigger")
MAX_HOOK_BYTES = 2 * 1024 * 1024


@hooks_bp.post("/hooks/gta6-editor")
async def gta6_editor_updated():
    body = await request.get_data()
    if len(body) > MAX_HOOK_BYTES:
        abort(413)
    secret = os.environ.get("GTA6_HOOK_SECRET", "").encode()
    if not secret:
        abort(503)
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not hmac.compare_digest(expected, signature):
        abort(403)
    if request.headers.get("X-GitHub-Event") == "push":
        payload = json.loads(body)
        if payload.get("ref") == "refs/heads/main":
            SYNC_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
            SYNC_TRIGGER.write_text(str(time.time_ns()), encoding="ascii")
    return "", 204
