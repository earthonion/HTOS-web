import csv
import io
import os

import httpx
from quart import Blueprint, Response, jsonify, render_template, request, session

from auth import login_required
from models import get_db
from services import error_codes as _ec
from services.filesystem import search_filesystem
from services.functions import search_functions
from services.ps5_updates import PS5_UPDATE_HOSTS
from services.ps5_updates import lookup as ps5_updates_lookup
from services.titles import search_titles

VALID_URL_HOSTS = {
    "gs2.ww.prod.dl.playstation.net",
    "sgst.prod.dl.playstation.net",
    "zeus.dl.playstation.net",
    "ares.dl.playstation.net",
    "gs2-ww-prod.psn.akadns.net",
}

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


@tools_bp.route("/tools/ps5-updates")
async def ps5_updates():
    """PS5 title update lookup — wraps andshrew/PS5-Updates-Python."""
    url = request.args.get("url", "").strip()
    result = None
    error = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            error = "URL must start with http:// or https://"
        elif parsed.hostname not in PS5_UPDATE_HOSTS:
            error = f"URL host must be one of: {', '.join(sorted(PS5_UPDATE_HOSTS))}"
        else:
            try:
                result = await ps5_updates_lookup(url)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
    return await render_template(
        "tools_ps5_updates.html",
        url=url,
        result=result,
        error=error,
    )


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
        "tools_syscalls.html",
        q=q,
        results=results,
        total=len(all_sc),
        platform=platform,
    )


@tools_bp.route("/tools/api/syscall-search")
async def api_syscall_search():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "ps4")
    if platform not in ("ps4", "ps5"):
        platform = "ps4"
    results = _search_syscalls(q, platform) if q else _all_syscalls(platform)
    enriched = [{**r, "man_url": syscall_man_url(r["name"])} for r in results]
    return jsonify({"results": enriched})


@tools_bp.route("/tools/error-lookup")
async def error_lookup():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "all")
    if platform not in ("all", "ps4", "ps5"):
        platform = "all"
    filt = None if platform == "all" else platform
    results = _ec.search(q, platform=filt, limit=100) if q else []
    counts = _ec.total_by_platform()
    return await render_template(
        "tools_error_lookup.html",
        q=q,
        platform=platform,
        results=results,
        total=_ec.total(),
        counts_all=_ec.total(),
        counts_ps4=counts.get("ps4", 0),
        counts_ps5=counts.get("ps5", 0),
    )


@tools_bp.route("/tools/api/error-search")
async def api_error_search():
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    if platform not in ("ps4", "ps5"):
        platform = None
    if not q or len(q) < 2:
        return jsonify({"results": []})
    return jsonify({"results": _ec.search(q, platform=platform, limit=100)})


@tools_bp.route("/tools/sample-saves")
async def sample_saves():
    q = request.args.get("q", "").strip()
    results = []
    total = 0
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM sample_saves")
        total = (await cursor.fetchone())[0]
        if q and len(q) >= 2:
            like = f"%{q}%"
            cursor = await db.execute(
                "SELECT id, title_id, save_dir_name, title, platform, region, save_type, created_at FROM sample_saves "
                "WHERE title_id LIKE ? OR title LIKE ? ORDER BY title LIMIT 50",
                (like, like),
            )
        else:
            cursor = await db.execute(
                "SELECT id, title_id, save_dir_name, title, platform, region, save_type, created_at FROM sample_saves "
                "ORDER BY created_at DESC LIMIT 50"
            )
        results = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    return await render_template(
        "tools_sample_saves.html", q=q, results=results, total=total
    )


@tools_bp.route("/tools/api/sample-saves-search")
async def api_sample_saves_search():
    q = request.args.get("q", "").strip()
    db = await get_db()
    try:
        if q and len(q) >= 2:
            like = f"%{q}%"
            cursor = await db.execute(
                "SELECT id, title_id, save_dir_name, title, platform, region, save_type FROM sample_saves "
                "WHERE title_id LIKE ? OR title LIKE ? ORDER BY title LIMIT 20",
                (like, like),
            )
        else:
            cursor = await db.execute(
                "SELECT id, title_id, save_dir_name, title, platform, region, save_type FROM sample_saves "
                "ORDER BY created_at DESC LIMIT 20"
            )
        results = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    return jsonify({"results": results})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/icon")
async def sample_save_icon(sample_id):
    from quart import abort, send_file

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT title_id, save_dir_name FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    save_dir_name = row["save_dir_name"] or ""
    icon_name = (
        f"{row['title_id']}_{save_dir_name}.png"
        if save_dir_name
        else f"{row['title_id']}.png"
    )
    icon_path = os.path.join("workspace", "savedb_samples", "icons", icon_name)

    if not os.path.isfile(icon_path):
        abort(404)

    return await send_file(icon_path, mimetype="image/png")


@tools_bp.route("/tools/sample-saves/<int:sample_id>/download")
@login_required
async def sample_save_download(sample_id):
    import re

    from quart import abort, send_file

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT title_id, save_dir_name, title, save_path FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row or not os.path.isfile(row["save_path"]):
        abort(404)

    safe_title = (
        re.sub(r"[^\w\s\-]", "", row["title"]).strip().replace(" ", "_")
        if row["title"]
        else ""
    )
    dir_suffix = f"_{row['save_dir_name']}" if row["save_dir_name"] else ""
    if safe_title:
        filename = f"{safe_title}_{row['title_id']}{dir_suffix}_sample.zip"
    else:
        filename = f"{row['title_id']}{dir_suffix}_sample.zip"

    response = await send_file(
        row["save_path"], as_attachment=True, attachment_filename=filename
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@tools_bp.route("/tools/sample-saves/<int:sample_id>/delete", methods=["POST"])
@login_required
async def sample_save_delete(sample_id):
    from quart import abort

    if not session.get("is_admin"):
        abort(403)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT save_path, title_id, save_dir_name FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
        if not row:
            abort(404)

        # Delete zip file
        if row["save_path"] and os.path.isfile(row["save_path"]):
            os.remove(row["save_path"])

        # Delete icon
        from services.samples import ICONS_DIR

        icon_name = (
            f"{row['title_id']}_{row['save_dir_name']}.png"
            if row["save_dir_name"]
            else f"{row['title_id']}.png"
        )
        icon_path = os.path.join(ICONS_DIR, icon_name)
        if os.path.isfile(icon_path):
            os.remove(icon_path)

        await db.execute("DELETE FROM sample_saves WHERE id = ?", (sample_id,))
        await db.commit()
    finally:
        await db.close()

    return jsonify({"ok": True})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/binwalk")
@login_required
async def sample_save_binwalk(sample_id):
    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT binwalk_output FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    return jsonify({"output": row["binwalk_output"] or "(no binwalk data)"})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/file")
@login_required
async def sample_save_file(sample_id):
    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT file_output FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    return jsonify({"output": row["file_output"] or "(no file data)"})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/strings")
@login_required
async def sample_save_strings(sample_id):
    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT strings_output FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    return jsonify({"output": row["strings_output"] or "(no strings data)"})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/xxd")
@login_required
async def sample_save_xxd(sample_id):
    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT xxd_output FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    return jsonify({"output": row["xxd_output"] or "(no xxd data)"})


@tools_bp.route("/tools/sample-saves/<int:sample_id>/entropy")
@login_required
async def sample_save_entropy(sample_id):
    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT entropy_output FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        abort(404)

    return jsonify({"output": row["entropy_output"] or "(no entropy data)"})


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


# Sony-specific prefixes that won't have FreeBSD man pages
_SONY_PREFIXES = (
    "regmgr_",
    "jitshm_",
    "dl_get_",
    "evf_",
    "osem_",
    "namedobj_",
    "budget_",
    "opmc_",
    "mdbg_",
    "dynlib_",
    "dmem_",
    "blockpool_",
    "ipmimgr_",
    "physhm_",
    "app_state_",
    "app_save",
    "app_restore",
    "saved_app_",
    "apr_",
    "fsc2h_",
    "streamwrite",
    "acinfo_",
    "ampr_",
    "workspace_",
    "sandbox_path",
    "randomized_path",
    "rdup",
    "workaround",
    "is_development_mode",
    "get_self_auth_info",
    "get_authinfo",
    "mname",
    "is_in_sandbox",
    "set_vm_container",
    "debug_init",
    "suspend_process",
    "resume_process",
    "prepare_to_suspend",
    "prepare_to_resume",
    "process_terminate",
    "get_paging_stats",
    "get_proc_type",
    "get_resident",
    "set_gpo",
    "get_gpo",
    "get_vm_map_timestamp",
    "get_cpu_usage",
    "mmap_dmem",
    "resume_internal_hdd",
    "set_timezone_info",
    "set_phys_fmem_limit",
    "utc_to_localtime",
    "localtime_to_utc",
    "set_uevt",
    "get_map_statistics",
    "set_chicken_switches",
    "get_kernel_mem",
    "get_sdk_compiled",
    "get_ppr_sdk",
    "notify_app_event",
    "ioreq",
    "openintr",
    "get_bio_usage",
    "get_page_table_stats",
    "reserve_2mb_page",
    "cpumode_yield",
    "virtual_query",
    "batch_map",
    "query_memory_protection",
    "get_phys_page_size",
    "begin_app_mount",
    "end_app_mount",
    "suspend_system",
    "free_stack",
    "test_debug_rwmem",
    "mtypeprotect",
    "netcontrol",
    "netabort",
    "netgetsockinfo",
    "socketex",
    "socketclose",
    "netgetiflist",
    "kqueueex",
    "aio_create",
    "aio_get_data",
    "aio_init",
    "aio_multi_cancel",
    "aio_multi_delete",
    "aio_multi_poll",
    "aio_multi_wait",
    "aio_submit",
    "aio_submit_cmd",
    "dl_notify_event",
    "eport_close",
    "eport_create",
    "eport_delete",
    "eport_open",
    "eport_trigger",
    "sblock_create",
    "sblock_delete",
    "sblock_enter",
    "sblock_exit",
    "sblock_xenter",
    "sblock_xexit",
    "thr_get_ucontext",
    "thr_resume_ucontext",
    "thr_set_ucontext",
    "thr_suspend_ucontext",
    "getpath_fromaddr",
    "getpath_fromfd",
    "signasleep",
    "nasleep",
)

# Syscall names with no FreeBSD man page (verified against man.freebsd.org)
_NO_MAN_PAGE = {
    "acl_aclcheck_fd",
    "acl_aclcheck_file",
    "acl_aclcheck_link",
    "acl_delete_fd",
    "acl_delete_file",
    "acl_delete_link",
    "acl_get_fd",
    "acl_get_file",
    "acl_get_link",
    "acl_set_fd",
    "acl_set_file",
    "acl_set_link",
    "afs3_syscall",
    "asyncdaemon",
    "cap_get_fd",
    "cap_get_file",
    "cap_get_proc",
    "cap_new",
    "cap_rights_get",
    "cap_set_fd",
    "cap_set_file",
    "cap_set_proc",
    "clock_getcpuclockid2",
    "execv",
    "extattrctl",
    "getdescriptor",
    "getdomainname",
    "getdopt",
    "gethostid",
    "gethostname",
    "getkerninfo",
    "getpagesize",
    "gssd_syscall",
    "kmq_open",
    "kmq_setattr",
    "kmq_tify",
    "kmq_timedreceive",
    "kmq_timedsend",
    "kmq_unlink",
    "kmq_notify",
    "kse_create",
    "kse_exit",
    "kse_release",
    "kse_switchin",
    "kse_thr_interrupt",
    "kse_wakeup",
    "ksem_close",
    "ksem_destroy",
    "ksem_getvalue",
    "ksem_init",
    "ksem_open",
    "ksem_post",
    "ksem_timedwait",
    "ksem_trywait",
    "ksem_unlink",
    "ksem_wait",
    "ktimer_create",
    "ktimer_delete",
    "ktimer_getoverrun",
    "ktimer_gettime",
    "ktimer_settime",
    "lfs_bmapv",
    "lfs_markv",
    "lfs_segclean",
    "lfs_segwait",
    "mac_execve",
    "mac_get_fd",
    "mac_get_file",
    "mac_get_link",
    "mac_get_pid",
    "mac_get_proc",
    "mac_set_fd",
    "mac_set_file",
    "mac_set_link",
    "mac_set_proc",
    "mac_syscall",
    "msgsys",
    "netbsd_lchown",
    "netbsd_msync",
    "newreboot",
    "nfsclnt",
    "nfstat",
    "nlm_syscall",
    "nlstat",
    "nnpfs_syscall",
    "nstat",
    "numa_getaffinity",
    "numa_setaffinity",
    "openbsd_poll",
    "ovadvise",
    "pdwait4",
    "quota",
    "resuba",
    "sem_lock",
    "sem_wakeup",
    "semconfig",
    "semsys",
    "sfork",
    "setdescriptor",
    "setdomainname",
    "setdopt",
    "sethostid",
    "sethostname",
    "setugid",
    "shmsys",
    "sstk",
    "thr_create",
    "thr_sleep",
    "thr_wakeup",
    "thr_get_name",
    "vhangup",
    "vlimit",
    "vread",
    "vtimes",
    "vtrace",
    "vwrite",
    "xfstat",
    "xlstat",
    "xstat",
}

# Remap syscall names to their correct FreeBSD man page query + section
_MAN_REMAP = {
    "exit": ("_exit", 2),
    "obreak": ("brk", 2),
    "mkd": ("mkdir", 2),
    "mkdat": ("mkdirat", 2),
    "umtx_op": ("_umtx_op", 2),
    "getcontext": ("getcontext", 3),
    "setcontext": ("setcontext", 3),
    "swapcontext": ("swapcontext", 3),
    "uname": ("uname", 3),
    "yield": ("yield", 3),
    "sysctl": ("sysctl", 3),
    "getcwd": ("getcwd", 3),
}


def syscall_man_url(name):
    """Return FreeBSD man page URL for a syscall name, or None if no page exists."""
    clean = name
    if clean.startswith("sys_"):
        clean = clean[4:]

    # Strip leading underscores for lookup
    bare = clean.lstrip("_")

    # Skip obviously non-linkable entries
    if bare.startswith(("nosys", "number", "obsolete", "obs_", "compat", "lkmnosys")):
        return None

    # Skip Sony-specific syscalls
    for prefix in _SONY_PREFIXES:
        if bare.startswith(prefix):
            return None

    # Skip verified broken names
    if bare in _NO_MAN_PAGE:
        return None

    # Check for known remappings
    if bare in _MAN_REMAP:
        query, sektion = _MAN_REMAP[bare]
        return f"https://man.freebsd.org/cgi/man.cgi?query={query}&sektion={sektion}"

    return f"https://man.freebsd.org/cgi/man.cgi?query={bare}&sektion=2"


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
        return jsonify(
            {
                "ok": False,
                "error": "URL validation failed — none of the sampled URLs resolved.",
            }
        ), 400

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
        is_admin=session.get("is_admin", False),
    )


@tools_bp.route("/tools/entitlements/csv")
@login_required
async def entitlements_csv():
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


import json as _json
import struct as _struct
import subprocess as _subprocess
import tempfile as _tempfile
from pathlib import Path as _Path

from PIL import Image as _PILImage
from quart import send_file as _send_file

_PS5_SDK = "/opt/ps5-payload-sdk"
_TEMPLATE_C = _Path("/opt/htos/static/main_template.c")
_WEBKIT_BUILD_DIR = "/var/lib/htos/ps5builds"


def _generate_bc7_dds(png_path, dds_path):
    import etcpak

    img = _PILImage.open(png_path).convert("RGBA")
    w, h = img.size
    bc7 = etcpak.compress_bc7(img.tobytes(), w, h)
    # DDS header: 4 magic + 124 header + 20 DX10 ext = 148 bytes
    hdr = bytearray(148)
    _struct.pack_into("<4s", hdr, 0, b"DDS ")
    _struct.pack_into("<I", hdr, 4, 124)  # dwSize
    _struct.pack_into(
        "<I", hdr, 8, 0x81007
    )  # dwFlags: CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    _struct.pack_into("<I", hdr, 12, h)  # dwHeight
    _struct.pack_into("<I", hdr, 16, w)  # dwWidth
    _struct.pack_into("<I", hdr, 20, len(bc7))  # dwPitchOrLinearSize
    # 24: dwDepth = 0
    _struct.pack_into("<I", hdr, 28, 1)  # dwMipMapCount
    # 32-75: dwReserved1[11] = zeros
    _struct.pack_into("<I", hdr, 76, 32)  # ddspf.dwSize
    _struct.pack_into("<I", hdr, 80, 0x4)  # ddspf.dwFlags: FOURCC
    _struct.pack_into("<4s", hdr, 84, b"DX10")  # ddspf.dwFourCC
    # 88-104: ddspf remainder = zeros
    _struct.pack_into("<I", hdr, 108, 0x1000)  # dwCaps: TEXTURE
    # 112-124: dwCaps2..dwReserved2 = zeros
    # DX10 extended header at offset 128
    _struct.pack_into("<I", hdr, 128, 98)  # dxgiFormat: BC7_UNORM
    _struct.pack_into("<I", hdr, 132, 3)  # resourceDimension: TEXTURE2D
    _struct.pack_into("<I", hdr, 136, 0)  # miscFlag
    _struct.pack_into("<I", hdr, 140, 1)  # arraySize
    _struct.pack_into("<I", hdr, 144, 0)  # miscFlags2
    with open(dds_path, "wb") as f:
        f.write(hdr)
        f.write(bc7)


def _resize_save(src_path, dst_path, size):
    img = _PILImage.open(src_path).convert("RGBA")
    img = img.resize(size, _PILImage.LANCZOS)
    img.save(dst_path, "PNG")


def _strip_asset(src, asset_name):
    lines = src.split("\n")
    out = []
    skip = 0
    for line in lines:
        if skip > 0:
            skip -= 1
            continue
        dotted = asset_name.replace("_", ".")
        if (asset_name in line or dotted in line) and (
            "INCASSET" in line or "install_file" in line
        ):
            if "install_file" in line:
                skip = 4
            continue
        out.append(line)
    return "\n".join(out)


@tools_bp.route("/webkit-shortcut-elf")
async def webkit_shortcut():
    return await render_template("tools_webkit_shortcut.html")


@tools_bp.route("/webkit-shortcut-elf/build", methods=["POST"])
async def webkit_shortcut_build():
    log = []
    try:
        form = await request.form
        files = await request.files
        title = form.get("title", "Relapse")
        title_id = form.get("title_id", "RLPS10001").upper()
        deeplink = form.get("deeplink", "")
        elf_name = form.get("elf_name", "install.elf")
        install_msg = form.get("install_msg", "Installing...")
        success_msg = form.get("success_msg", "Installed!")
        include_pic0 = "include_pic0" in form
        gen_dds = "include_pic0" in form
        fullscreen = "fullscreen" in form
        include_bgm = "include_bgm" in form
        app_type = form.get("app_type", "media")
        icon_svg_data = form.get("icon_svg_data", "")

        icon_file = files.get("icon")
        pic1_file = files.get("pic1")
        pic0_file = files.get("pic0")
        bgm_file = files.get("bgm")

        _default_icon = "/opt/htos/static/default_icon0.png"
        _default_pic1 = "/opt/htos/static/default_pic1.png"
        has_custom_icon = icon_file and icon_file.filename
        has_custom_pic1 = pic1_file and pic1_file.filename
        if len(title_id) != 9:
            log.append(f"ERROR: Title ID must be 9 chars (got {len(title_id)})")
            return jsonify(ok=False, log=log)

        bd = _tempfile.mkdtemp(prefix="ps5app_")
        sd = os.path.join(bd, "sce_sys")
        os.makedirs(sd)

        if icon_svg_data.startswith("data:"):
            import base64 as _b64

            _header, _b64data = icon_svg_data.split(",", 1)
            icon_tmp = os.path.join(bd, "icon_upload")
            with open(icon_tmp, "wb") as _f:
                _f.write(_b64.b64decode(_b64data))
            log.append("Using picked icon...")
        elif has_custom_icon:
            icon_tmp = os.path.join(bd, "icon_upload")
            await icon_file.save(icon_tmp)
        else:
            icon_tmp = _default_icon
        if has_custom_pic1:
            pic1_tmp = os.path.join(bd, "pic1_upload")
            await pic1_file.save(pic1_tmp)
        else:
            pic1_tmp = _default_pic1

        import time as _time_mod

        os.makedirs(_WEBKIT_BUILD_DIR, exist_ok=True)
        for _old in os.listdir(_WEBKIT_BUILD_DIR):
            _p = os.path.join(_WEBKIT_BUILD_DIR, _old)
            try:
                if (
                    os.path.isfile(_p)
                    and _time_mod.time() - os.path.getmtime(_p) > 3600
                ):
                    os.remove(_p)
            except OSError:
                pass

        log.append("Writing param.json...")
        if fullscreen:
            pj = {
                "applicationCategoryType": 66048,
                "titleId": title_id,
                "localizedParameters": {
                    "defaultLanguage": "en-US",
                    "en-US": {"titleName": title},
                },
                "downloadDataSize": 256,
                "webAppUri": deeplink,
            }
        else:
            pj = {
                "titleId": title_id,
                "deeplinkUri": deeplink,
                "localizedParameters": {
                    "defaultLanguage": "en-US",
                    "en-US": {"titleName": title},
                },
            }
            if app_type == "media":
                pj["applicationCategoryType"] = 65536
        with open(os.path.join(sd, "param.json"), "w") as f:
            _json.dump(pj, f, indent=4)

        log.append("Resizing icon to 512x512...")
        _resize_save(icon_tmp, os.path.join(sd, "icon0.png"), (512, 512))

        log.append("Resizing background to 1920x1080...")
        _resize_save(pic1_tmp, os.path.join(sd, "pic1.png"), (1920, 1080))

        has_dds = False
        has_pic0 = False

        if include_pic0:
            if pic0_file and pic0_file.filename:
                pic0_tmp = os.path.join(bd, "pic0_upload")
                await pic0_file.save(pic0_tmp)
            else:
                pic0_tmp = pic1_tmp
            log.append("Resizing splash to 1920x1080...")
            _resize_save(pic0_tmp, os.path.join(sd, "pic0.png"), (1920, 1080))
            has_pic0 = True
            if gen_dds:
                log.append("Generating BC7 DDS...")
                _generate_bc7_dds(
                    os.path.join(sd, "pic0.png"), os.path.join(sd, "pic0.dds")
                )
                has_dds = True
                dds_sz = os.path.getsize(os.path.join(sd, "pic0.dds"))
                log.append(f"DDS generated ({dds_sz / 1024 / 1024:.1f} MB)")

        has_bgm = False
        if include_bgm:
            if bgm_file and bgm_file.filename:
                bgm_tmp = os.path.join(bd, "bgm_upload")
                await bgm_file.save(bgm_tmp)
                log.append("Converting audio to AT9...")
                at9_proc = _subprocess.run(
                    [
                        "python3",
                        "/opt/ps5-at9-converter/ps5_at9.py",
                        "-i",
                        bgm_tmp,
                        "-o",
                        os.path.join(sd, "snd0.at9"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if at9_proc.returncode != 0:
                    log.append(f"ERROR: AT9 conversion failed")
                    if at9_proc.stderr.strip():
                        log.append(at9_proc.stderr.strip())
                    return jsonify(ok=False, log=log)
                at9_sz = os.path.getsize(os.path.join(sd, "snd0.at9"))
                log.append(f"AT9 generated ({at9_sz / 1024:.0f} KB)")
            else:
                import shutil as _shutil

                _shutil.copy2(
                    "/opt/htos/static/default_snd0.at9", os.path.join(sd, "snd0.at9")
                )
                log.append("Using default BGM chime")
            has_bgm = True

        log.append("Writing main.c...")
        src = _TEMPLATE_C.read_text()
        src = src.replace("@@INSTALL_MSG@@", install_msg)
        src = src.replace("@@SUCCESS_MSG@@", success_msg)
        if not has_dds:
            src = _strip_asset(src, "pic0_dds")
        if not has_pic0:
            src = _strip_asset(src, "pic0_png")
        if not has_bgm:
            src = _strip_asset(src, "snd0_at9")
        with open(os.path.join(bd, "main.c"), "w") as f:
            f.write(src)

        deps = "sce_sys/param.json sce_sys/icon0.png sce_sys/pic1.png"
        if has_dds:
            deps += " sce_sys/pic0.dds"
        if has_pic0:
            deps += " sce_sys/pic0.png"
        if has_bgm:
            deps += " sce_sys/snd0.at9"
        mf = (
            "ifdef PS5_PAYLOAD_SDK\n"
            "    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk\n"
            "else\n"
            "    $(error PS5_PAYLOAD_SDK is undefined)\n"
            "endif\n\n"
            f"ELF := {elf_name}\n"
            f"CFLAGS := -Wall -Werror -g -DTITLE_ID='\"{title_id}\"'\n"
            "LDADD  := -lSceIpmi -lSceAppInstUtil\n\n"
            "all: $(ELF)\n\n"
            "$(ELF): main.c\n"
            "\t$(CC) $(CFLAGS) $(LDADD) -o $@ $^\n\n"
            f"install.c: {deps}\n\n"
            "clean:\n"
            "\trm -f $(ELF)\n"
        )
        with open(os.path.join(bd, "Makefile"), "w") as f:
            f.write(mf)

        log.append("Compiling...")
        env = os.environ.copy()
        env["PS5_PAYLOAD_SDK"] = _PS5_SDK
        proc = _subprocess.run(
            ["make", "clean", "all"],
            cwd=bd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.stdout.strip():
            log.append(proc.stdout.strip())
        if proc.returncode != 0:
            log.append(f"ERROR: Build failed (exit {proc.returncode})")
            if proc.stderr.strip():
                log.append(proc.stderr.strip())
            return jsonify(ok=False, log=log)

        elf_path = os.path.join(bd, elf_name)
        if not os.path.exists(elf_path):
            log.append(f"ERROR: {elf_name} not found")
            return jsonify(ok=False, log=log)

        size_kb = os.path.getsize(elf_path) / 1024
        log.append(f"SUCCESS: {elf_name} ({size_kb:.0f} KB)")
        os.makedirs(_WEBKIT_BUILD_DIR, exist_ok=True)
        import shutil as _shutil_mod

        dest = os.path.join(_WEBKIT_BUILD_DIR, elf_name)
        _shutil_mod.copy2(elf_path, dest)
        src_zip = os.path.join(bd, "source.zip")
        import zipfile as _zf_mod

        with _zf_mod.ZipFile(src_zip, "w", _zf_mod.ZIP_DEFLATED) as zf:
            for name in ("main.c", "Makefile"):
                p = os.path.join(bd, name)
                if os.path.exists(p):
                    zf.write(p, name)
            sd2 = os.path.join(bd, "sce_sys")
            if os.path.isdir(sd2):
                for fname in os.listdir(sd2):
                    zf.write(os.path.join(sd2, fname), f"sce_sys/{fname}")
        src_dest = os.path.join(
            _WEBKIT_BUILD_DIR, elf_name.replace(".elf", "") + "-source.zip"
        )
        _shutil_mod.copy2(src_zip, src_dest)
        return jsonify(ok=True, log=log, elf=elf_name)

    except Exception as e:
        log.append(f"ERROR: {e}")
        return jsonify(ok=False, log=log)


@tools_bp.route("/webkit-shortcut-elf/download/<name>")
async def webkit_shortcut_download(name):
    path = os.path.join(_WEBKIT_BUILD_DIR, name)
    if not os.path.exists(path):
        return "No build available", 404
    return await _send_file(path, as_attachment=True, attachment_filename=name)


@tools_bp.route("/webkit-shortcut-elf/download-source")
async def webkit_shortcut_download_source():
    elf_name = request.args.get("elf", "shortcut-install.elf")
    zip_name = elf_name.replace(".elf", "") + "-source.zip"
    path = os.path.join(_WEBKIT_BUILD_DIR, zip_name)
    if not os.path.exists(path):
        return "No build available", 404
    return await _send_file(path, as_attachment=True, attachment_filename=zip_name)
