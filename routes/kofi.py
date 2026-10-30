"""
Ko-fi webhook receiver.

Ko-fi POSTs application/x-www-form-urlencoded with a single `data` field
that contains the payment JSON. Docs:
    https://help.ko-fi.com/hc/en-us/articles/360004162298

Important behaviours:
  - Must return 200 OK or Ko-fi will retry the same message_id.
  - `is_public=false` means we MUST hide the message when displaying
    publicly (but can still record the row).
  - Subscription renewals send is_subscription_payment=true; only the
    first one has is_first_subscription_payment=true.

Configure the URL in the Ko-fi dashboard:
    https://garlicsaves.com/api/kofi-webhook
plus the Verification Token from the same page in /opt/htos/.env as
KOFI_VERIFICATION_TOKEN.
"""

import json
import logging
import os

from quart import Blueprint, request

from models import get_db

logger = logging.getLogger(__name__)

kofi_bp = Blueprint("kofi", __name__)


@kofi_bp.post("/api/kofi-webhook")
async def kofi_webhook():
    expected_token = os.environ.get("KOFI_VERIFICATION_TOKEN", "")
    form = await request.form
    raw = form.get("data", "")

    if not raw:
        logger.warning("kofi: empty data field; form keys=%s", list(form.keys()))
        return "no data", 200

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("kofi: bad JSON: %s; first 200 chars: %r", e, raw[:200])
        return "bad json", 200

    if expected_token and payload.get("verification_token") != expected_token:
        logger.warning("kofi: token mismatch from sender; ignoring")
        return "ok", 200

    message_id = payload.get("message_id")
    if not message_id:
        logger.warning("kofi: payload missing message_id")
        return "ok", 200

    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR IGNORE INTO kofi_supporters (
                kofi_message_id, type, from_name, message, amount, currency,
                is_public, is_subscription_payment, tier_name, timestamp,
                raw_data, is_manual
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                message_id,
                payload.get("type"),
                (payload.get("from_name") or "").strip() or "Anonymous",
                payload.get("message") or "",
                str(payload.get("amount") or ""),
                payload.get("currency") or "",
                1 if payload.get("is_public") else 0,
                1 if payload.get("is_subscription_payment") else 0,
                payload.get("tier_name") or "",
                payload.get("timestamp") or "",
                raw,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    logger.info(
        "kofi: stored %s from %r (type=%s, public=%s)",
        message_id,
        payload.get("from_name"),
        payload.get("type"),
        payload.get("is_public"),
    )
    return "ok", 200
