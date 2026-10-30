import os

from dotenv import load_dotenv
from markupsafe import Markup
from quart import Quart, Response, render_template, session

from utils.extras import group_supporters

load_dotenv()


def create_app():
    app = Quart(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32).hex()
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.getenv("MAX_UPLOAD_SIZE", str(11 * 1024 * 1024 * 1024))
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("HTTPS", "0") == "1"

    from auth import auth_bp
    from models import init_db
    from routes.admin_web import admin_web_bp
    from routes.api import api_bp
    from routes.api_v1 import api_v1_bp
    from routes.bo1 import bo1_bp
    from routes.chunked import chunked_bp
    from routes.contribute import contribute_bp
    from routes.convert import convert_bp
    from routes.createsave import createsave_bp
    from routes.decrypt import decrypt_bp
    from routes.encrypt import encrypt_bp
    from routes.jobs import jobs_bp
    from routes.kofi import kofi_bp
    from routes.luac0re import luac0re_bp
    from routes.main import main_bp
    from routes.quickcodes import quickcodes_bp
    from routes.reregion import reregion_bp
    from routes.resign import resign_bp
    from routes.rest_api import rest_bp
    from routes.savedb import savedb_bp
    from routes.saveeditor import saveeditor_bp
    from routes.tools import tools_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(resign_bp)
    app.register_blueprint(decrypt_bp)
    app.register_blueprint(encrypt_bp)
    app.register_blueprint(reregion_bp)
    app.register_blueprint(createsave_bp)
    app.register_blueprint(convert_bp)
    app.register_blueprint(quickcodes_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(contribute_bp)
    app.register_blueprint(api_v1_bp)
    app.register_blueprint(rest_bp)
    app.register_blueprint(chunked_bp)
    app.register_blueprint(admin_web_bp)
    app.register_blueprint(luac0re_bp)
    app.register_blueprint(bo1_bp)
    app.register_blueprint(savedb_bp)
    app.register_blueprint(saveeditor_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(kofi_bp)

    @app.errorhandler(400)
    async def bad_request(e):
        return await render_template(
            "error.html",
            code=400,
            title="Bad Request",
            message="The request could not be understood.",
        ), 400

    @app.errorhandler(403)
    async def forbidden(e):
        return await render_template(
            "error.html",
            code=403,
            title="Forbidden",
            message="You don't have permission to access this.",
        ), 403

    @app.errorhandler(404)
    async def not_found(e):
        return await render_template(
            "error.html",
            code=404,
            title="Not Found",
            message="The page you're looking for doesn't exist.",
        ), 404

    @app.errorhandler(413)
    async def too_large(e):
        return await render_template(
            "error.html",
            code=413,
            title="File Too Large",
            message="The uploaded file exceeds the size limit.",
        ), 413

    @app.errorhandler(429)
    async def rate_limited(e):
        return await render_template(
            "error.html",
            code=429,
            title="Too Many Requests",
            message="Slow down. Please try again later.",
        ), 429

    @app.errorhandler(500)
    async def server_error(e):
        return await render_template(
            "error.html",
            code=500,
            title="Server Error",
            message="Something went wrong. Please try again later.",
        ), 500

    @app.route("/ads.txt")
    async def ads_txt():
        client_id = os.getenv("ADSENSE_CLIENT_ID", "")
        if not client_id:
            return Response("", status=404)
        pub_id = client_id.replace("ca-", "")
        return Response(
            f"google.com, {pub_id}, DIRECT, f08c47fec0942fa0\n",
            mimetype="text/plain",
        )

    @app.template_filter("to_usb_id")
    def to_usb_id(account_id):
        """Convert stored big-endian account ID to USB folder format (little-endian)."""
        if len(account_id) == 16:
            return "".join(reversed([account_id[i : i + 2] for i in range(0, 16, 2)]))
        return account_id

    @app.template_filter("to_rp_id")
    def to_rp_id(account_id):
        """Convert hex account ID to base64 remote play format (little-endian)."""
        import base64

        if len(account_id) == 16:
            try:
                usb_id = "".join(
                    reversed([account_id[i : i + 2] for i in range(0, 16, 2)])
                )
                return base64.b64encode(int(usb_id, 16).to_bytes(8, "little")).decode()
            except Exception:
                pass
        return ""

    @app.template_filter("syscall_man_url")
    def _syscall_man_url_filter(name):
        from routes.tools import syscall_man_url

        return syscall_man_url(name)

    import html
    import re as _re

    _URL_RE = _re.compile(r"""https?://[^\s<>\"']+""")

    def _linkify(text):
        if not text:
            return text
        text = str(text)
        out = []
        last = 0
        for m in _URL_RE.finditer(text):
            out.append(html.escape(text[last : m.start()]))
            raw = m.group(0)
            # strip trailing punct that's almost certainly not part of the URL
            stripped = raw.rstrip(".,;:)]}!?")
            tail = raw[len(stripped) :]
            out.append(
                f'<a href="{html.escape(stripped, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(stripped)}</a>'
                + html.escape(tail)
            )
            last = m.end()
        out.append(html.escape(text[last:]))
        return Markup("".join(out))

    app.jinja_env.filters["linkify"] = _linkify

    @app.before_request
    async def _bump_user_last_seen():
        # Bump users.last_seen for the signed-in viewer so the admin's
        # "Online now" stat is accurate. Throttled to at most one write
        # per 60s per user via the WHERE clause; skips static + healthcheck.
        from quart import request as _request
        from quart import session as _session

        uid = _session.get("user_id")
        if not uid:
            return
        ep = _request.endpoint or ""
        if ep == "static" or ep.startswith("api.next_job"):
            return
        from models import get_db

        try:
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE users SET last_seen = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND (last_seen IS NULL "
                    "                   OR last_seen < datetime('now','-60 seconds'))",
                    (uid,),
                )
                await db.commit()
            finally:
                await db.close()
        except Exception:
            pass

    @app.context_processor
    async def inject_worker_count():
        from models import get_db

        ps4_count = 0
        ps5_count = 0
        try:
            db = await get_db()
            try:
                # Count online INSTANCES via the sticky capability flags. A
                # dual-platform PS5 worker (which alternates ps4/ps5 polls)
                # counts in BOTH ps4 and ps5 totals — its is_ps5_capable and
                # is_ps4_capable flags both stay set across polls.
                cursor = await db.execute(
                    "SELECT "
                    "  SUM(CASE WHEN wi.is_ps5_capable = 1 THEN 1 ELSE 0 END) AS ps5, "
                    "  SUM(CASE WHEN wi.is_ps4_capable = 1 THEN 1 ELSE 0 END) AS ps4 "
                    "FROM worker_instances wi "
                    "JOIN worker_keys wk ON wk.id = wi.worker_key_id "
                    "WHERE wi.last_seen > datetime('now', '-300 seconds') "
                    "AND wk.is_active = 1 "
                    "AND (wk.suspended_until IS NULL OR wk.suspended_until <= datetime('now'))"
                )
                row = await cursor.fetchone()
                if row:
                    ps5_count = row["ps5"] or 0
                    ps4_count = row["ps4"] or 0
                # Plus active keys with recent heartbeat that have no instance row yet
                # (legacy / pre-instance-tracking workers — keep counts in sync).
                cursor = await db.execute(
                    "SELECT last_platform, COUNT(*) as cnt FROM worker_keys wk "
                    "WHERE last_used IS NOT NULL AND last_used > datetime('now', '-300 seconds') "
                    "AND is_active = 1 "
                    "AND (suspended_until IS NULL OR suspended_until <= datetime('now')) "
                    "AND NOT EXISTS (SELECT 1 FROM worker_instances wi WHERE wi.worker_key_id = wk.id "
                    "  AND wi.last_seen > datetime('now', '-300 seconds')) "
                    "GROUP BY last_platform"
                )
                for row in await cursor.fetchall():
                    if row["last_platform"] == "ps5":
                        ps5_count += row["cnt"]
                    else:
                        ps4_count += row["cnt"]
            finally:
                await db.close()
        except Exception:
            pass
        # Undismissed admin messages for current user (and broadcasts)
        admin_msgs = []
        uid = session.get("user_id")
        if uid:
            try:
                db = await get_db()
                try:
                    cursor = await db.execute(
                        "SELECT id, message, level FROM admin_messages "
                        "WHERE dismissed_at IS NULL AND (user_id = ? OR user_id IS NULL) "
                        "ORDER BY created_at DESC LIMIT 5",
                        (uid,),
                    )
                    admin_msgs = [dict(r) for r in await cursor.fetchall()]
                finally:
                    await db.close()
            except Exception:
                pass
        # Maintenance banner — written by /usr/local/bin/maint-banner-check
        # ~15 min before the auto-update window if security patches are pending.
        # File is removed post-upgrade. Cheap stat per request; no parse if absent.
        maint = None
        try:
            import json as _json

            with open("/var/lib/maint-banner/active.json") as _mf:
                maint = _json.load(_mf)
                if not maint.get("active"):
                    maint = None
        except (OSError, ValueError):
            pass

        footer_supporters = []
        footer_anonymous_count = 0
        try:
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT from_name, is_subscription_payment, tier_name "
                    "FROM kofi_supporters "
                    "WHERE LOWER(COALESCE(from_name, '')) != 'anonymous' "
                    "ORDER BY created_at DESC LIMIT 60"
                )
                footer_supporters = group_supporters(
                    [dict(r) for r in await cursor.fetchall()]
                )
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM kofi_supporters "
                    "WHERE LOWER(COALESCE(from_name, '')) = 'anonymous'"
                )
                footer_anonymous_count = (await cursor.fetchone())[0]
            finally:
                await db.close()
        except Exception:
            pass

        return dict(
            workers_online=ps4_count + ps5_count,
            ps4_workers=ps4_count,
            ps5_workers=ps5_count,
            ps5_workers_online=ps5_count > 0,
            adsense_client=os.getenv("ADSENSE_CLIENT_ID", ""),
            admin_messages=admin_msgs,
            maintenance=maint,
            footer_supporters=footer_supporters,
            footer_anonymous_count=footer_anonymous_count,
        )

    @app.before_serving
    async def startup():
        await init_db()
        for d in [
            "workspace/uploads",
            "workspace/results",
            "workspace/processing",
            "workspace/chunks",
            "workspace/savedb",
            "workspace/savedb_samples",
            "workspace/saveeditor",
        ]:
            os.makedirs(d, exist_ok=True)

        # Start TCP direct-connect worker server
        from config import TCP_WORKER_ENABLED, TCP_WORKER_PORT

        if TCP_WORKER_ENABLED:
            from services.tcp_worker import start_tcp_server

            try:
                app.tcp_server = await start_tcp_server(TCP_WORKER_PORT)
            except OSError as e:
                import logging

                logging.getLogger(__name__).error(
                    "TCP worker server failed to start: %s", e
                )

        # Start bridge fallback poller — hands stuck queued PS4
        # decrypt/encrypt jobs to the bridge worker when
        # has capacity. Worker pulls still win via atomic try_claim().
        import asyncio as _asyncio

        from services.jobs import bridge_fallback_poller

        app.bridge_poller_task = _asyncio.create_task(bridge_fallback_poller())

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("DEBUG", "0") == "1")
