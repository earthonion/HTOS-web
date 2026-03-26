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

CREATE TABLE IF NOT EXISTS sample_saves (
    id INTEGER PRIMARY KEY,
    title_id TEXT UNIQUE NOT NULL,
    title TEXT DEFAULT '',
    platform TEXT DEFAULT 'ps4',
    region TEXT DEFAULT '',
    save_path TEXT NOT NULL,
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
]


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already exists
        await db.commit()


async def get_db():
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    return db
