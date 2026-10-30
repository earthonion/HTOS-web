import aiosqlite

from config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    name TEXT NOT NULL,
    account_id TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    operation TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    result_path TEXT,
    error TEXT,
    params TEXT
);

CREATE TABLE IF NOT EXISTS worker_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    key TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    last_platform TEXT DEFAULT 'ps4'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invite_codes (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    created_by INTEGER REFERENCES users(id),
    used_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_stats (
    operation TEXT NOT NULL,
    worker_key_id INTEGER,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    PRIMARY KEY (operation, worker_key_id)
);

CREATE TABLE IF NOT EXISTS savedb_entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    title TEXT NOT NULL,
    title_id TEXT NOT NULL,
    description TEXT DEFAULT '',
    platform TEXT DEFAULT 'ps4',
    save_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    upvotes INTEGER DEFAULT 0,
    downvotes INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS savedb_votes (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER REFERENCES savedb_entries(id),
    user_id INTEGER REFERENCES users(id),
    vote INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entry_id, user_id)
);

CREATE TABLE IF NOT EXISTS entitlements (
    id INTEGER PRIMARY KEY,
    entitlement_id TEXT UNIQUE NOT NULL,
    title TEXT DEFAULT '',
    title_id TEXT DEFAULT '',
    package_url TEXT DEFAULT '',
    platform TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    contributed_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    sender_id INTEGER REFERENCES users(id),
    message TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    dismissed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admin_messages_user_undismissed
    ON admin_messages(user_id, dismissed_at);

CREATE TABLE IF NOT EXISTS worker_instances (
    id INTEGER PRIMARY KEY,
    worker_key_id INTEGER NOT NULL REFERENCES worker_keys(id),
    ip TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    online_since TIMESTAMP,
    last_platform TEXT DEFAULT 'ps4',
    jobs_done INTEGER DEFAULT 0,
    jobs_failed INTEGER DEFAULT 0,
    UNIQUE(worker_key_id, ip)
);

CREATE INDEX IF NOT EXISTS idx_worker_instances_key ON worker_instances(worker_key_id);

CREATE TABLE IF NOT EXISTS sample_saves (
    id INTEGER PRIMARY KEY,
    title_id TEXT NOT NULL,
    save_dir_name TEXT DEFAULT '',
    title TEXT DEFAULT '',
    platform TEXT DEFAULT 'ps4',
    region TEXT DEFAULT '',
    save_type TEXT DEFAULT '',
    binwalk_output TEXT DEFAULT '',
    file_output TEXT DEFAULT '',
    strings_output TEXT DEFAULT '',
    xxd_output TEXT DEFAULT '',
    entropy_output TEXT DEFAULT '',
    save_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(title_id, save_dir_name)
);


CREATE TABLE IF NOT EXISTS kofi_supporters (
    id INTEGER PRIMARY KEY,
    kofi_message_id TEXT UNIQUE,
    type TEXT,
    from_name TEXT,
    message TEXT,
    amount TEXT,
    currency TEXT,
    is_public INTEGER DEFAULT 1,
    is_subscription_payment INTEGER DEFAULT 0,
    tier_name TEXT,
    timestamp TEXT,
    raw_data TEXT,
    is_manual INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('invite_only', '0');
"""

MIGRATIONS = [
    "ALTER TABLE worker_keys ADD COLUMN last_platform TEXT DEFAULT 'ps4'",
    "ALTER TABLE worker_keys ADD COLUMN jobs_completed INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN worker_key_id INTEGER",
    "ALTER TABLE worker_keys ADD COLUMN suspended_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
    "ALTER TABLE worker_keys ADD COLUMN online_since TIMESTAMP",
    "ALTER TABLE jobs ADD COLUMN logs TEXT",
    "ALTER TABLE entitlements ADD COLUMN verified BOOLEAN DEFAULT NULL",
    "ALTER TABLE sample_saves ADD COLUMN save_type TEXT DEFAULT ''",
    "ALTER TABLE sample_saves ADD COLUMN save_dir_name TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN reset_code TEXT",
    "ALTER TABLE sample_saves ADD COLUMN binwalk_output TEXT DEFAULT ''",
    "ALTER TABLE sample_saves ADD COLUMN file_output TEXT DEFAULT ''",
    "ALTER TABLE sample_saves ADD COLUMN strings_output TEXT DEFAULT ''",
    "ALTER TABLE sample_saves ADD COLUMN xxd_output TEXT DEFAULT ''",
    "ALTER TABLE sample_saves ADD COLUMN entropy_output TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN banned BOOLEAN DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN worker_instance_id INTEGER",
    # Per-instance platform capability flags. Set whenever an instance polls
    # the matching ?platform=X queue. Backfill from current last_platform so
    # existing rows don't look non-capable immediately after migration.
    "ALTER TABLE worker_instances ADD COLUMN is_ps4_capable INTEGER DEFAULT 0",
    "ALTER TABLE worker_instances ADD COLUMN is_ps5_capable INTEGER DEFAULT 0",
    "UPDATE worker_instances SET is_ps4_capable = 1 WHERE last_platform = 'ps4'",
    "UPDATE worker_instances SET is_ps5_capable = 1 WHERE last_platform = 'ps5'",
    # Per-instance suspension. Lets us isolate a single buggy PS5 on the
    # shared global key without suspending every other user sharing it.
    "ALTER TABLE worker_instances ADD COLUMN suspended_until TIMESTAMP",
    # Failure-retry: requeue a failed job up to N times before final-failing.
    "ALTER TABLE jobs ADD COLUMN retry_count INTEGER DEFAULT 0",
    # Per-job duration timestamps for passive worker-speed measurement.
    # started_at  → stamped when worker first transitions to status=running
    # completed_at → stamped on status=done or status=failed (final-failed only)
    "ALTER TABLE jobs ADD COLUMN started_at TIMESTAMP",
    "ALTER TABLE jobs ADD COLUMN completed_at TIMESTAMP",
    # users.last_seen for the "Online now" admin stat. Bumped by a
    # before_request hook (throttled to one write per 60s per user).
    "ALTER TABLE users ADD COLUMN kofi_claim_code TEXT",
    "ALTER TABLE users ADD COLUMN supporter_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN last_seen TIMESTAMP",
]


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Recreate sample_saves if it lacks save_dir_name (constraint changed)
        try:
            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='sample_saves'"
            )
            row = await cursor.fetchone()
            if row and "save_dir_name" not in row[0]:
                await db.execute("DROP TABLE sample_saves")
        except Exception:
            pass

        await db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already exists
        await db.commit()


async def get_db():
    # WAL + tuned pragmas per-connection so writers dont block readers and
    # brief contention is retried in-driver instead of raising OperationalError.
    db = await aiosqlite.connect(DATABASE_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=30000")
    return db
