import os
import struct
import shutil
import uuid
import zipfile

from quart import Blueprint, render_template, request, session, redirect, url_for, flash, abort, send_file

from quart import jsonify

from auth import login_required, admin_required
from models import get_db
from services.jobs import create_job
from services.files import check_dangerous_files, check_zip_safety, DangerousFileError
from services.titles import lookup_title

savedb_bp = Blueprint("savedb", __name__)

SAVEDB_DIR = os.path.join("workspace", "savedb")
DELETE_THRESHOLD = -10
PER_PAGE = 20


def _find_and_validate_sfo(directory):
    """Find param.sfo in directory tree, validate it, and extract fields.
    Returns (sfo_fields, error_message).
    sfo_fields is a dict with keys like TITLE_ID, MAINTITLE, TITLE, SAVEDATA_DIRECTORY, etc.
    error_message is None if valid."""
    sfo_path = None
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower() == "param.sfo":
                sfo_path = os.path.join(root, f)
                break
        if sfo_path:
            break

    if not sfo_path:
        return None, "No param.sfo found. This doesn't appear to be a valid PS4/PS5 save."

    with open(sfo_path, "rb") as fh:
        data = fh.read()

    if len(data) < 20 or data[:4] != b'\x00PSF':
        return None, "Invalid param.sfo (bad magic bytes). This doesn't appear to be a valid save."

    key_off = struct.unpack_from('<I', data, 8)[0]
    data_off = struct.unpack_from('<I', data, 12)[0]
    count = struct.unpack_from('<I', data, 16)[0]
    fields = {}
    for i in range(count):
        base = 20 + i * 16
        if base + 16 > len(data):
            return None, "Invalid param.sfo (truncated index table)."
        k_off = struct.unpack_from('<H', data, base)[0]
        fmt = struct.unpack_from('<H', data, base + 2)[0]
        d_len = struct.unpack_from('<I', data, base + 4)[0]
        d_off = struct.unpack_from('<I', data, base + 12)[0]
        try:
            end = data.index(b'\x00', key_off + k_off)
            key = data[key_off + k_off:end].decode()
        except (ValueError, UnicodeDecodeError):
            continue
        if fmt == 0x0204:
            fields[key] = data[data_off + d_off:data_off + d_off + d_len].rstrip(b'\x00').decode()
        elif key == "SAVEDATA_BLOCKS":
            fields[key] = struct.unpack_from('<Q', data, data_off + d_off)[0]

    if "TITLE_ID" not in fields:
        return None, "Invalid param.sfo (missing TITLE_ID). This doesn't appear to be a valid save."

    return fields, None


@savedb_bp.route("/savedb")
@login_required
async def browse():
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PER_PAGE
    user_id = session["user_id"]

    db = await get_db()
    try:
        if q:
            like = f"%{q}%"
            cursor = await db.execute(
                "SELECT e.*, u.username as contributor FROM savedb_entries e "
                "JOIN users u ON e.user_id = u.id "
                "WHERE e.title LIKE ? OR e.title_id LIKE ? "
                "ORDER BY (e.upvotes - e.downvotes) DESC, e.created_at DESC LIMIT ? OFFSET ?",
                (like, like, PER_PAGE + 1, offset)
            )
        else:
            cursor = await db.execute(
                "SELECT e.*, u.username as contributor FROM savedb_entries e "
                "JOIN users u ON e.user_id = u.id "
                "ORDER BY (e.upvotes - e.downvotes) DESC, e.created_at DESC LIMIT ? OFFSET ?",
                (PER_PAGE + 1, offset)
            )
        rows = [dict(r) for r in await cursor.fetchall()]
        has_next = len(rows) > PER_PAGE
        entries = rows[:PER_PAGE]

        # Get user's votes
        if entries:
            entry_ids = [e["id"] for e in entries]
            placeholders = ",".join("?" * len(entry_ids))
            cursor = await db.execute(
                f"SELECT entry_id, vote FROM savedb_votes "
                f"WHERE user_id = ? AND entry_id IN ({placeholders})",
                [user_id] + entry_ids
            )
            user_votes = {r["entry_id"]: r["vote"] for r in await cursor.fetchall()}
        else:
            user_votes = {}
    finally:
        await db.close()

    return await render_template("savedb_browse.html",
                                 entries=entries, q=q, page=page, has_next=has_next,
                                 user_votes=user_votes)


@savedb_bp.route("/savedb/api/lookup_title/<title_id>")
@login_required
async def api_lookup_title(title_id):
    name = lookup_title(title_id.strip().upper())
    return jsonify({"name": name})


@savedb_bp.route("/savedb/contribute", methods=["GET", "POST"])
@login_required
async def contribute():
    if request.method == "POST":
        user_id = session["user_id"]
        form = await request.form
        description = form.get("description", "").strip()
        files_dict = await request.files
        zipfile_upload = files_dict.get("zipfile")
        folder_files = files_dict.getlist("folder_files")
        is_folder_upload = bool(folder_files and folder_files[0].filename)

        if not is_folder_upload and (not zipfile_upload or not zipfile_upload.filename):
            await flash("Please upload a zip file or folder.", "error")
            return await render_template("savedb_contribute.html")

        # Save files to temp dir
        temp_id = str(uuid.uuid4())
        temp_dir = os.path.join("workspace", "uploads", str(user_id), temp_id)
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if is_folder_upload:
                for f in folder_files:
                    if not f.filename:
                        continue
                    rel_path = f.filename
                    dest = os.path.join(temp_dir, rel_path)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    await f.save(dest)
                check_dangerous_files(temp_dir)
            else:
                zip_path = os.path.join(temp_dir, zipfile_upload.filename)
                await zipfile_upload.save(zip_path)
                try:
                    check_zip_safety(zip_path)
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(temp_dir)
                    os.unlink(zip_path)
                except zipfile.BadZipFile:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    await flash("Invalid zip file.", "error")
                    return await render_template("savedb_contribute.html")
                check_dangerous_files(temp_dir)
        except DangerousFileError as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            await flash(str(e), "error")
            return await render_template("savedb_contribute.html")

        # Validate param.sfo and extract fields
        fields, sfo_error = _find_and_validate_sfo(temp_dir)
        if sfo_error:
            shutil.rmtree(temp_dir, ignore_errors=True)
            await flash(sfo_error, "error")
            return await render_template("savedb_contribute.html")

        # Extract title_id and determine platform from prefix
        title_id = fields["TITLE_ID"]
        prefix = title_id[:4].upper()
        platform = "ps5" if prefix == "PPSA" else "ps4"

        # Get game title: try titles DB first, fall back to SFO MAINTITLE/TITLE
        title = lookup_title(title_id)
        if not title:
            title = fields.get("MAINTITLE") or fields.get("TITLE") or title_id

        # Insert DB entry with auto-upvote
        db = await get_db()
        try:
            cursor = await db.execute(
                "INSERT INTO savedb_entries (user_id, title, title_id, description, platform, save_path, upvotes) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (user_id, title, title_id, description, platform, "")
            )
            entry_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO savedb_votes (entry_id, user_id, vote) VALUES (?, ?, 1)",
                (entry_id, user_id)
            )
            await db.commit()
        finally:
            await db.close()

        # Move files to permanent savedb location
        save_dir = os.path.join(SAVEDB_DIR, str(entry_id))
        os.makedirs(save_dir, exist_ok=True)
        for fname in os.listdir(temp_dir):
            shutil.move(os.path.join(temp_dir, fname), os.path.join(save_dir, fname))
        shutil.rmtree(temp_dir, ignore_errors=True)

        # Update save_path
        db = await get_db()
        try:
            await db.execute(
                "UPDATE savedb_entries SET save_path = ? WHERE id = ?",
                (save_dir, entry_id)
            )
            await db.commit()
        finally:
            await db.close()

        await flash("Save submitted!", "success")
        return redirect(url_for("savedb.detail", entry_id=entry_id))

    return await render_template("savedb_contribute.html")


@savedb_bp.route("/savedb/<int:entry_id>")
@login_required
async def detail(entry_id):
    user_id = session["user_id"]
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT e.*, u.username as contributor FROM savedb_entries e "
            "JOIN users u ON e.user_id = u.id WHERE e.id = ?",
            (entry_id,)
        )
        entry = await cursor.fetchone()
        if not entry:
            abort(404)
        entry = dict(entry)

        # Get user's vote
        cursor = await db.execute(
            "SELECT vote FROM savedb_votes WHERE entry_id = ? AND user_id = ?",
            (entry_id, user_id)
        )
        vote_row = await cursor.fetchone()
        user_vote = vote_row["vote"] if vote_row else 0
    finally:
        await db.close()

    return await render_template("savedb_detail.html", entry=entry, user_vote=user_vote)


@savedb_bp.route("/savedb/<int:entry_id>/vote", methods=["POST"])
@login_required
async def vote(entry_id):
    user_id = session["user_id"]
    form = await request.form
    vote_dir = form.get("vote")
    if vote_dir not in ("up", "down"):
        abort(400)

    vote_val = 1 if vote_dir == "up" else -1

    db = await get_db()
    try:
        # Check entry exists
        cursor = await db.execute(
            "SELECT user_id, save_path FROM savedb_entries WHERE id = ?", (entry_id,)
        )
        entry = await cursor.fetchone()
        if not entry:
            abort(404)

        # Check existing vote
        cursor = await db.execute(
            "SELECT vote FROM savedb_votes WHERE entry_id = ? AND user_id = ?",
            (entry_id, user_id)
        )
        existing = await cursor.fetchone()

        if existing:
            if existing["vote"] == vote_val:
                await db.execute(
                    "DELETE FROM savedb_votes WHERE entry_id = ? AND user_id = ?",
                    (entry_id, user_id)
                )
            else:
                await db.execute(
                    "UPDATE savedb_votes SET vote = ? WHERE entry_id = ? AND user_id = ?",
                    (vote_val, entry_id, user_id)
                )
        else:
            await db.execute(
                "INSERT INTO savedb_votes (entry_id, user_id, vote) VALUES (?, ?, ?)",
                (entry_id, user_id, vote_val)
            )

        # Recalculate cached counts
        cursor = await db.execute(
            "SELECT COALESCE(SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END), 0) as up, "
            "COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) as down "
            "FROM savedb_votes WHERE entry_id = ?",
            (entry_id,)
        )
        counts = await cursor.fetchone()
        up, down = counts["up"], counts["down"]

        # Auto-delete at -10
        if up - down <= DELETE_THRESHOLD:
            if entry["save_path"] and os.path.isdir(entry["save_path"]):
                shutil.rmtree(entry["save_path"], ignore_errors=True)
            await db.execute("DELETE FROM savedb_votes WHERE entry_id = ?", (entry_id,))
            await db.execute("DELETE FROM savedb_entries WHERE id = ?", (entry_id,))
            await db.commit()
            await flash("Save was removed by community votes.", "info")
            return redirect(url_for("savedb.browse"))

        await db.execute(
            "UPDATE savedb_entries SET upvotes = ?, downvotes = ? WHERE id = ?",
            (up, down, entry_id)
        )
        await db.commit()
    finally:
        await db.close()

    return redirect(request.referrer or url_for("savedb.detail", entry_id=entry_id))


@savedb_bp.route("/savedb/<int:entry_id>/encrypt", methods=["GET", "POST"])
@login_required
async def encrypt(entry_id):
    user_id = session["user_id"]

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT e.*, u.username as contributor FROM savedb_entries e "
            "JOIN users u ON e.user_id = u.id WHERE e.id = ?",
            (entry_id,)
        )
        entry = await cursor.fetchone()
        if not entry:
            await flash("Save not found.", "error")
            return redirect(url_for("savedb.browse"))
        entry = dict(entry)

        cursor = await db.execute(
            "SELECT id, name, account_id FROM profiles WHERE user_id = ?", (user_id,)
        )
        profiles = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    if request.method == "POST":
        form = await request.form
        profile_id = form.get("profile_id")

        if not profile_id:
            await flash("Please select a profile.", "error")
            return await render_template("savedb_resign.html", entry=entry, profiles=profiles)

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT account_id FROM profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id)
            )
            profile = await cursor.fetchone()
        finally:
            await db.close()

        if not profile:
            await flash("Invalid profile.", "error")
            return await render_template("savedb_resign.html", entry=entry, profiles=profiles)

        # Copy files from savedb to workspace (preserve directory structure)
        temp_id = str(uuid.uuid4())
        upload_dir = os.path.join("workspace", "uploads", str(user_id), temp_id)
        shutil.copytree(entry["save_path"], upload_dir)

        # Read savename and saveblocks from param.sfo
        platform = entry["platform"]
        savename = None
        saveblocks = None
        sfo_path = None
        for root, dirs, files in os.walk(upload_dir):
            for f in files:
                if f.lower() == "param.sfo":
                    sfo_path = os.path.join(root, f)
                    break
            if sfo_path:
                break

        if sfo_path:
            with open(sfo_path, "rb") as fh:
                data = fh.read()
            if len(data) > 20 and data[:4] == b'\x00PSF':
                key_off = struct.unpack_from('<I', data, 8)[0]
                data_off = struct.unpack_from('<I', data, 12)[0]
                count = struct.unpack_from('<I', data, 16)[0]
                for i in range(count):
                    base = 20 + i * 16
                    k_off = struct.unpack_from('<H', data, base)[0]
                    fmt = struct.unpack_from('<H', data, base + 2)[0]
                    d_len = struct.unpack_from('<I', data, base + 4)[0]
                    d_off = struct.unpack_from('<I', data, base + 12)[0]
                    end = data.index(b'\x00', key_off + k_off)
                    key = data[key_off + k_off:end].decode()
                    if key == "SAVEDATA_DIRECTORY" and fmt == 0x0204:
                        savename = data[data_off + d_off:data_off + d_off + d_len].rstrip(b'\x00').decode()
                    elif key == "SAVEDATA_BLOCKS":
                        saveblocks = struct.unpack_from('<Q', data, data_off + d_off)[0]

        params = {
            "account_id": profile["account_id"],
            "upload_dir": upload_dir,
            "platform": platform,
        }
        if savename:
            params["savename"] = savename
        if saveblocks:
            params["saveblocks"] = saveblocks

        job = await create_job(user_id, "encrypt", params, ready=True)

        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("savedb_resign.html", entry=entry, profiles=profiles)


@savedb_bp.route("/savedb/<int:entry_id>/download")
@login_required
async def download(entry_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT save_path, title, title_id FROM savedb_entries WHERE id = ?",
            (entry_id,)
        )
        entry = await cursor.fetchone()
    finally:
        await db.close()

    if not entry or not entry["save_path"] or not os.path.isdir(entry["save_path"]):
        await flash("Save not found.", "error")
        return redirect(url_for("savedb.browse"))

    # Build zip on disk (preserve directory structure)
    zip_name = f"{entry['title_id']}_{entry['title']}.zip".replace(" ", "_")
    zip_path = os.path.join("workspace", "uploads", f"savedb_dl_{entry_id}_{uuid.uuid4().hex[:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for root, dirs, files in os.walk(entry["save_path"]):
            for fname in files:
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, entry["save_path"])
                zf.write(fpath, arcname)

    return await send_file(zip_path, as_attachment=True, attachment_filename=zip_name)


@savedb_bp.route("/savedb/<int:entry_id>/delete", methods=["POST"])
@login_required
async def delete(entry_id):
    user_id = session["user_id"]

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT save_path FROM savedb_entries WHERE id = ? AND user_id = ?",
            (entry_id, user_id)
        )
        entry = await cursor.fetchone()
        if not entry:
            await flash("Cannot delete this entry.", "error")
            return redirect(url_for("savedb.browse"))

        if entry["save_path"] and os.path.isdir(entry["save_path"]):
            shutil.rmtree(entry["save_path"], ignore_errors=True)

        await db.execute("DELETE FROM savedb_votes WHERE entry_id = ?", (entry_id,))
        await db.execute("DELETE FROM savedb_entries WHERE id = ?", (entry_id,))
        await db.commit()
    finally:
        await db.close()

    await flash("Save entry deleted.", "success")
    return redirect(url_for("savedb.browse"))
