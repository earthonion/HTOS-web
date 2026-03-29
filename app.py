import os

from dotenv import load_dotenv
from quart import Quart, Response, render_template

load_dotenv()


def create_app():
    app = Quart(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32).hex()
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.getenv("MAX_UPLOAD_SIZE", str(2 * 1024 * 1024 * 1024))
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("HTTPS", "0") == "1"

    from auth import auth_bp
    from models import init_db
    from routes.admin_web import admin_web_bp
    from routes.api import api_bp
    from routes.chunked import chunked_bp
    from routes.contribute import contribute_bp
    from routes.convert import convert_bp
    from routes.createsave import createsave_bp
    from routes.decrypt import decrypt_bp
    from routes.encrypt import encrypt_bp
    from routes.jobs import jobs_bp
    from routes.luac0re import luac0re_bp
    from routes.main import main_bp
    from routes.quickcodes import quickcodes_bp
    from routes.reregion import reregion_bp
    from routes.resign import resign_bp
    from routes.rest_api import rest_bp
    from routes.savedb import savedb_bp
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
    app.register_blueprint(rest_bp)
    app.register_blueprint(chunked_bp)
    app.register_blueprint(admin_web_bp)
    app.register_blueprint(luac0re_bp)
    app.register_blueprint(savedb_bp)
    app.register_blueprint(tools_bp)

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

    @app.template_filter("syscall_man_url")
    def _syscall_man_url_filter(name):
        from routes.tools import syscall_man_url

        return syscall_man_url(name)

    @app.context_processor
    async def inject_worker_count():
        from models import get_db

        ps4_count = 0
        ps5_count = 0
        try:
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT last_platform, COUNT(*) as cnt FROM worker_keys "
                    "WHERE is_active = 1 AND last_used IS NOT NULL "
                    "AND last_used > datetime('now', '-300 seconds') "
                    "AND (suspended_until IS NULL OR suspended_until <= datetime('now')) "
                    "GROUP BY last_platform"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    if row["last_platform"] == "ps5":
                        ps5_count = row["cnt"]
                    else:
                        ps4_count = row["cnt"]
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
        ]:
            os.makedirs(d, exist_ok=True)

        # Start TCP direct-connect worker server
        from config import TCP_WORKER_ENABLED, TCP_WORKER_PORT

        if TCP_WORKER_ENABLED:
            from services.tcp_worker import start_tcp_server

            app.tcp_server = await start_tcp_server(TCP_WORKER_PORT)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("DEBUG", "0") == "1")
