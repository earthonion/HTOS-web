#!/bin/bash
# No set -e: file cleanup must always run even if later commands fail

BASE=/opt/htos
SAVES=$BASE/UserSaves
WORKSPACE=$BASE/workspace

# --- File cleanup (runs first, must not be blocked) ---
mkdir -p "$SAVES" "$WORKSPACE" 2>/dev/null || true

# Delete files >30 min old under UserSaves + workspace, EXCEPT paths still
# referenced by non-terminal jobs (pending/queued/running) — otherwise a
# slow-to-drain queue lets us delete an upload_dir mid-flight and the bridge
# 404s on /api/worker/jobs/<id>/files. Python instead of `find` because we
# need to consult the jobs DB for the in-use set.
cd "$BASE"
.venv/bin/python <<'PY'
import json, os, sqlite3, time

BASE = "/opt/htos"
SAVES = os.path.join(BASE, "UserSaves")
WORKSPACE = os.path.join(BASE, "workspace")
CUTOFF = time.time() - 30 * 60
PROTECT_PREFIXES = (os.path.join(WORKSPACE, "savedb"),
                    os.path.join(WORKSPACE, "savedb_samples"))

in_use = set()
try:
    with sqlite3.connect(os.path.join(BASE, "htos_web.db")) as db:
        db.row_factory = sqlite3.Row
        for r in db.execute(
            "SELECT params FROM jobs WHERE status IN ('pending','queued','running')"
        ):
            try:
                p = json.loads(r["params"] or "{}")
            except Exception:
                continue
            for k in ("upload_dir", "saves_dir", "sample_dir"):
                v = p.get(k)
                if v: in_use.add(os.path.normpath(v))
except sqlite3.Error:
    pass  # if DB is unreachable, fall through to age-only rules

def is_in_use(path):
    for d in in_use:
        if path == d or path.startswith(d + os.sep):
            return True
    return False

def is_protected(path):
    return any(path == pp or path.startswith(pp + os.sep) for pp in PROTECT_PREFIXES)

def sweep(root, respect_protect=False):
    for cur, dirs, files in os.walk(root, topdown=False):
        if respect_protect and is_protected(cur):
            continue
        for f in files:
            p = os.path.join(cur, f)
            try:
                if os.path.getmtime(p) < CUTOFF and not is_in_use(p):
                    os.unlink(p)
            except OSError:
                pass
        try:
            if cur != root and not os.listdir(cur) \
               and not (respect_protect and is_protected(cur)):
                os.rmdir(cur)
        except OSError:
            pass

sweep(SAVES)
sweep(WORKSPACE, respect_protect=True)
PY

# Recreate subdirectories that the app + bridge expect (sweep prunes empties)
mkdir -p "$WORKSPACE/uploads" "$WORKSPACE/results" "$WORKSPACE/savedb" \
         "$WORKSPACE/savedb_samples" "$WORKSPACE/bridge-tmp" 2>/dev/null || true

# --- DB cleanup (best-effort, failures won't block restart) ---

# Delete abandoned pending jobs (no upload_dir or upload_dir missing, older than 10 min)
.venv/bin/python -c "
import asyncio, aiosqlite, json, os
async def main():
    async with aiosqlite.connect('$BASE/htos_web.db') as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(\"SELECT id, params FROM jobs WHERE status = 'pending' AND created_at < datetime('now', '-10 minutes')\")
        expired = []
        for r in rows:
            p = json.loads(r['params']) if r['params'] else {}
            upload_dir = p.get('upload_dir') or p.get('saves_dir')
            if not upload_dir or not os.path.isdir(upload_dir):
                expired.append(r['id'])
        if expired:
            placeholders = ','.join('?' * len(expired))
            await db.execute(f\"DELETE FROM jobs WHERE id IN ({placeholders})\", expired)
            await db.commit()
            print(f'Deleted {len(expired)} abandoned pending job(s).')
asyncio.run(main())
" || echo "Warning: abandoned job cleanup failed"

# Delete jobs older than 30 min (matching file-cleanup TTL). Roll their
# stats into job_stats before delete so aggregates survive. Jobs whose
# result files were preserved by the file-cleanup step keep their row.
.venv/bin/python -c "
import asyncio, aiosqlite
AGE = 'datetime(\'now\', \'-30 minutes\')'
COND = f'COALESCE(completed_at, started_at, created_at) < {AGE}'
async def main():
    async with aiosqlite.connect('$BASE/htos_web.db') as db:
        await db.execute(
            'INSERT INTO job_stats (operation, worker_key_id, done, failed, total) '
            'SELECT operation, COALESCE(worker_key_id, 0), '
            '  SUM(CASE WHEN status=\'done\' THEN 1 ELSE 0 END), '
            '  SUM(CASE WHEN status=\'failed\' THEN 1 ELSE 0 END), '
            '  COUNT(*) '
            f'FROM jobs WHERE {COND} '
            'GROUP BY operation, COALESCE(worker_key_id, 0) '
            'ON CONFLICT(operation, worker_key_id) DO UPDATE SET '
            '  done = job_stats.done + excluded.done, '
            '  failed = job_stats.failed + excluded.failed, '
            '  total = job_stats.total + excluded.total'
        )
        c = await db.execute(f'DELETE FROM jobs WHERE {COND}')
        await db.commit()
        print(f'Deleted {c.rowcount} job(s) older than 30 min.')
asyncio.run(main())
" || echo "Warning: age-based job cleanup failed"

.venv/bin/python -c "
import asyncio, aiosqlite
async def main():
    async with aiosqlite.connect('$BASE/htos_web.db') as db:
        c = await db.execute('DELETE FROM worker_keys WHERE is_active = 0')
        await db.commit()
        print(f'Deleted {c.rowcount} revoked worker key(s).')
asyncio.run(main())
" || echo "Warning: revoke cleanup failed"

chown -R www-data:www-data "$SAVES" "$WORKSPACE" 2>/dev/null || true

