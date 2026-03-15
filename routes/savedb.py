import os
import shutil
import uuid
import zipfile

from quart import Blueprint, render_template, request, session, redirect, url_for, flash, abort, send_file

from auth import login_required, admin_required
from models import get_db
from services.jobs import create_job

savedb_bp = Blueprint("savedb", __name__)

SAVEDB_DIR = os.path.join("workspace", "savedb")
DELETE_THRESHOLD = -10
PER_PAGE = 20


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


@savedb_bp.route("/savedb/contribute", methods=["GET", "POST"])
@login_required
async def contribute():
    if request.method == "POST":
        user_id = session["user_id"]
        form = await request.form
        title = form.get("title", "").strip()
        title_id = form.get("title_id", "").strip().upper()
        description = form.get("description", "").strip()
        platform = form.get("platform", "ps4")
        files_dict = await request.files
        zipfile_upload = files_dict.get("zipfile")
        folder_files = files_dict.getlist("folder_files")
        is_folder_upload = bool(folder_files and folder_files[0].filename)

        if not title:
            await flash("Game title is required.", "error")
            return await render_template("savedb_contribute.html")
        if not title_id:
            await flash("Title ID (CUSA) is required.", "error")
            return await render_template("savedb_contribute.html")
        if not is_folder_upload and (not zipfile_upload or not zipfile_upload.filename):
            await flash("Please upload a zip file or folder.", "error")
            return await render_template("savedb_contribute.html")
        if platform not in ("ps4", "ps5"):
            platform = "ps4"

        # Save files to temp dir
        temp_id = str(uuid.uuid4())
        temp_dir = os.path.join("workspace", "uploads", str(user_id), temp_id)
        os.makedirs(temp_dir, exist_ok=True)

        if is_folder_upload:
            for f in folder_files:
                if not f.filename:
                    continue
                fname = os.path.basename(f.filename)
                await f.save(os.path.join(temp_dir, fname))
        else:
            zip_path = os.path.join(temp_dir, zipfile_upload.filename)
            await zipfile_upload.save(zip_path)
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(temp_dir)
                os.unlink(zip_path)
            except zipfile.BadZipFile:
                shutil.rmtree(temp_dir, ignore_errors=True)
                await flash("Invalid zip file.", "error")
                return await render_template("savedb_contribute.html")

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

        # Copy files from savedb to workspace
        temp_id = str(uuid.uuid4())
        upload_dir = os.path.join("workspace", "uploads", str(user_id), temp_id)
        os.makedirs(upload_dir, exist_ok=True)

        for fname in os.listdir(entry["save_path"]):
            src = os.path.join(entry["save_path"], fname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(upload_dir, fname))

        platform = entry["platform"]
        job = await create_job(user_id, "encrypt", {
            "account_id": profile["account_id"],
            "upload_dir": upload_dir,
            "platform": platform,
        }, ready=True)

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

    # Build zip on disk
    zip_name = f"{entry['title_id']}_{entry['title']}.zip".replace(" ", "_")
    zip_path = os.path.join("workspace", "uploads", f"savedb_dl_{entry_id}_{uuid.uuid4().hex[:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for fname in os.listdir(entry["save_path"]):
            fpath = os.path.join(entry["save_path"], fname)
            if os.path.isfile(fpath):
                zf.write(fpath, fname)

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
