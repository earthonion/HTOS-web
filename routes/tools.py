import csv
import io
import os

import httpx
from quart import Blueprint, Response, jsonify, render_template, request, session

from auth import login_required
from models import get_db
from services.filesystem import search_filesystem
from services.functions import search_functions
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


@tools_bp.route("/tools/sample-saves/<int:sample_id>/binwalk")
@login_required
async def sample_save_binwalk(sample_id):
    import asyncio
    import tempfile
    import zipfile

    from quart import abort

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT save_path FROM sample_saves WHERE id = ?",
            (sample_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row or not os.path.isfile(row["save_path"]):
        abort(404)

    output_lines = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(row["save_path"], "r") as zf:
            zf.extractall(tmp)

        for root, _, files in os.walk(tmp):
            rel_root = os.path.relpath(root, tmp)
            if "sce_sys" in rel_root.split(os.sep):
                continue
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, tmp)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "binwalk",
                        fpath,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                    out = stdout.decode("utf-8", errors="replace").strip()
                    output_lines.append(f"=== {rel} ===")
                    output_lines.append(out if out else "(no results)")
                    output_lines.append("")
                except Exception as e:
                    output_lines.append(f"=== {rel} ===")
                    output_lines.append(f"Error: {e}")
                    output_lines.append("")

    return jsonify({"output": "\n".join(output_lines)})


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
