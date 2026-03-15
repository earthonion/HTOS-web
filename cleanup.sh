#!/bin/bash
set -e

BASE=/opt/htos
SAVES=$BASE/UserSaves
WORKSPACE=$BASE/workspace

mkdir -p "$SAVES" "$WORKSPACE"

find "$SAVES" -mindepth 1 -delete
find "$WORKSPACE" -mindepth 1 -not -path "$WORKSPACE/savedb" -not -path "$WORKSPACE/savedb/*" -delete

# Recreate subdirectories that the app expects
mkdir -p "$WORKSPACE/uploads" "$WORKSPACE/results" "$WORKSPACE/savedb"

cd "$BASE"

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
"

.venv/bin/python admin.py clear-jobs
.venv/bin/python -c "
import asyncio, aiosqlite
async def main():
    async with aiosqlite.connect('$BASE/htos_web.db') as db:
        c = await db.execute('DELETE FROM worker_keys WHERE is_active = 0')
        await db.commit()
        print(f'Deleted {c.rowcount} revoked worker key(s).')
asyncio.run(main())
"

chown -R www-data:www-data "$SAVES" "$WORKSPACE"

systemctl restart htos
