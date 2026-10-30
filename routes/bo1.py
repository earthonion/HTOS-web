"""Black Ops 1 PRF editor.

Flow:
  1. GET/POST /bo1            — upload page: pick profile, drop save. POST
     creates a decrypt job with bo1_editor=True and redirects to the job
     status page. When the decrypt completes, api.py's chain hook stages
     the files, and jobs.py's job_status redirects the user to
     /bo1/edit/<decrypt_job_id>.
  2. GET  /bo1/edit/<job_id>                — file browser + inline editor.
  3. GET  /bo1/edit/<job_id>/prf?rel=...    — return the decrypted text of a
                                              single PRF (used by the JS
                                              client to load into the editor).
  4. POST /bo1/edit/<job_id>/save           — save edited text back (re-
                                              encrypts single PRF in place).
  5. POST /bo1/edit/<job_id>/encrypt        — queue the encrypt job and
                                              redirect to its status page.
"""
import json
import os
from pathlib import Path

from quart import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)

from auth import login_required
from models import get_db
from services.bo1 import (
    decrypt_prf_bytes, list_prf_files, queue_encrypt_from_stage,
    write_prf_from_text,
)
from services.files import (
    DangerousFileError,
    FileTooLargeError,
    InvalidSaveFilesError,
    detect_platform_in_dir,
    extract_account_id,
    resolve_chunked_uploads,
    save_uploaded_files,
    validate_save_pairs,
)
from services.jobs import create_job

bo1_bp = Blueprint("bo1", __name__)

PRESETS_DIR = Path(__file__).resolve().parent.parent / "prebuilt" / "bo1"


def _list_presets():
    """Return [{slug, name, size}] for every *.txt in prebuilt/bo1/."""
    out = []
    if PRESETS_DIR.is_dir():
        for p in sorted(PRESETS_DIR.glob("*.txt")):
            out.append({
                "slug": p.stem,
                "name": p.stem.replace("_", " ").replace("-", " ").title(),
                "size": p.stat().st_size,
            })
    return out


async def _load_bo1_decrypt_job(job_id: str, user_id: int):
    """Fetch a bo1-editor decrypt job for the current user. 404 if missing,
    wrong user, wrong op, or not in editor mode."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, operation, status, result_path, params FROM jobs "
            "WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row or row["operation"] != "decrypt":
        return None
    params = json.loads(row["params"] or "{}")
    if not params.get("bo1_editor"):
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "result_path": row["result_path"],
        "params": params,
    }


def _resolve_stage(job) -> Path | None:
    """Return the stage dir path for a bo1-editor job if it exists on disk."""
    stage = job["params"].get("bo1_stage_dir")
    if not stage:
        return None
    p = Path(stage)
    if not p.is_dir():
        return None
    return p


def _safe_stage_child(stage: Path, rel: str) -> Path | None:
    """Resolve `rel` under stage, refusing anything that escapes the tree."""
    if not rel:
        return None
    target = (stage / rel).resolve()
    try:
        target.relative_to(stage.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


@bo1_bp.route("/bo1", methods=["GET", "POST"])
@login_required
async def bo1():
    user_id = session["user_id"]

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, account_id FROM profiles WHERE user_id = ?", (user_id,)
        )
        profiles = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    if request.method == "POST":
        form = await request.form
        files = (await request.files).getlist("saves")
        upload_ids_json = form.get("upload_ids")
        profile_id = form.get("profile_id")

        if not profile_id:
            await flash("Select a profile.", "error")
            return await render_template("bo1.html", profiles=profiles)
        if not upload_ids_json and (not files or not files[0].filename):
            await flash(
                "Upload your encrypted BO1 save (save file + its .bin sealed key).",
                "error",
            )
            return await render_template("bo1.html", profiles=profiles)

        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT account_id FROM profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            )
            row = await cursor.fetchone()
        finally:
            await db.close()
        if not row:
            await flash("Invalid profile.", "error")
            return await render_template("bo1.html", profiles=profiles)
        target_account_id = row["account_id"]

        job = await create_job(user_id, "decrypt", {}, ready=False)
        try:
            if upload_ids_json:
                import json as _json
                upload_ids = _json.loads(upload_ids_json)
                upload_dir = await resolve_chunked_uploads(
                    upload_ids, user_id, job.job_id
                )
            else:
                upload_dir = await save_uploaded_files(files, user_id, job.job_id)
        except FileTooLargeError as e:
            await flash(f"Save file too large: {e}.", "error")
            return await render_template("bo1.html", profiles=profiles)
        except DangerousFileError as e:
            await flash(str(e), "error")
            return await render_template("bo1.html", profiles=profiles)

        platform = detect_platform_in_dir(upload_dir)
        if platform != "ps4":
            await flash(
                "The BO1 editor only supports PS4 saves. Upload the encrypted "
                "PS4 save file plus its .bin sealed key.",
                "error",
            )
            return await render_template("bo1.html", profiles=profiles)
        try:
            validate_save_pairs(upload_dir)
        except InvalidSaveFilesError as e:
            await flash(str(e), "error")
            return await render_template("bo1.html", profiles=profiles)

        params = {
            "upload_dir": upload_dir,
            "platform": "ps4",
            "include_sce_sys": True,
            "bo1_editor": True,
            "bo1_target_account": target_account_id,
        }
        acct = extract_account_id(upload_dir, "ps4")
        if acct:
            params["sfo_account_id"] = acct

        await job.update_params(params)
        job.logger.info(
            "BO1 editor: decrypting; PRF browser will open when the worker finishes."
        )
        await job.set_status("queued")
        return redirect(url_for("jobs.job_status", job_id=job.job_id))

    return await render_template("bo1.html", profiles=profiles)


@bo1_bp.route("/bo1/edit/<job_id>")
@login_required
async def bo1_edit(job_id):
    user_id = session["user_id"]
    job = await _load_bo1_decrypt_job(job_id, user_id)
    if not job:
        abort(404)
    if job["status"] != "done":
        # Decrypt hasn't finished yet — bounce back to the status page so the
        # user sees the SSE-driven progress log.
        return redirect(url_for("jobs.job_status", job_id=job_id))

    stage = _resolve_stage(job)
    if not stage:
        await flash(
            "The decrypted save was cleared (files older than 30 min are swept). "
            "Re-run the decrypt to start over.",
            "error",
        )
        return redirect(url_for("bo1.bo1"))

    prfs = list_prf_files(stage)
    target = job["params"].get("bo1_target_account", "")

    return await render_template(
        "bo1_edit.html",
        job_id=job_id,
        prfs=prfs,
        presets=_list_presets(),
        target_account_id=target,
    )


@bo1_bp.route("/bo1/edit/<job_id>/prf")
@login_required
async def bo1_edit_load(job_id):
    """Return the decrypted text of a single PRF (JSON: {rel, text, size})."""
    user_id = session["user_id"]
    job = await _load_bo1_decrypt_job(job_id, user_id)
    if not job or job["status"] != "done":
        abort(404)
    stage = _resolve_stage(job)
    if not stage:
        abort(404)
    rel = request.args.get("rel", "")
    target = _safe_stage_child(stage, rel)
    if not target:
        abort(404)
    blob = target.read_bytes()
    return jsonify({
        "rel": rel,
        "size": len(blob),
        "text": decrypt_prf_bytes(blob),
    })


@bo1_bp.route("/bo1/edit/<job_id>/preset")
@login_required
async def bo1_edit_preset(job_id):
    """Return preset text (JSON: {slug, text}). Guards on user auth even
    though presets are public — keeps the surface area small."""
    user_id = session["user_id"]
    job = await _load_bo1_decrypt_job(job_id, user_id)
    if not job:
        abort(404)
    slug = request.args.get("slug", "")
    if not slug or not slug.replace("_", "").replace("-", "").isalnum():
        abort(400)
    fp = PRESETS_DIR / f"{slug}.txt"
    if not fp.is_file():
        abort(404)
    return jsonify({"slug": slug, "text": fp.read_text()})


@bo1_bp.route("/bo1/edit/<job_id>/save", methods=["POST"])
@login_required
async def bo1_edit_save(job_id):
    user_id = session["user_id"]
    job = await _load_bo1_decrypt_job(job_id, user_id)
    if not job or job["status"] != "done":
        abort(404)
    stage = _resolve_stage(job)
    if not stage:
        abort(404)
    data = await request.get_json() or {}
    rel = data.get("rel", "")
    text = data.get("text", "")
    target = _safe_stage_child(stage, rel)
    if not target:
        abort(404)
    write_prf_from_text(target, text)
    # Touch parent dirs so the cleanup TTL restarts on this save action.
    os.utime(target, None)
    for parent in target.parents:
        if parent == stage.parent:
            break
        os.utime(parent, None)
    return jsonify({"ok": True, "size": target.stat().st_size})


@bo1_bp.route("/bo1/edit/<job_id>/encrypt", methods=["POST"])
@login_required
async def bo1_edit_encrypt(job_id):
    user_id = session["user_id"]
    job = await _load_bo1_decrypt_job(job_id, user_id)
    if not job or job["status"] != "done":
        abort(404)
    stage = _resolve_stage(job)
    if not stage:
        await flash("Decrypted save no longer on disk — re-run decrypt.", "error")
        return redirect(url_for("bo1.bo1"))
    target = job["params"].get("bo1_target_account")
    if not target:
        await flash("Missing target profile on this job.", "error")
        return redirect(url_for("bo1.bo1"))

    encrypt_job_id = await queue_encrypt_from_stage(
        user_id=user_id,
        stage_dir=str(stage),
        target_account_id=target,
        decrypt_job_id=job_id,
    )
    return redirect(url_for("jobs.job_status", job_id=encrypt_job_id))
