#!/bin/bash
set -e

BASE=/opt/htos
SAVES=$BASE/UserSaves
WORKSPACE=$BASE/workspace

mkdir -p "$SAVES" "$WORKSPACE"

find "$SAVES" -mindepth 1 -delete
find "$WORKSPACE" -mindepth 1 -delete

# Recreate subdirectories that the app expects
mkdir -p "$WORKSPACE/uploads" "$WORKSPACE/results"

cd "$BASE"
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
