# HTOS-web

Web interface for HTOS PS4/PS5 save management. Replaces the Discord bot + NiceGUI desktop app with an internet-facing Quart web server.

## Features

- **Resign** - Re-sign saves to a different PSN account
- **Decrypt** - Decrypt PS4/PS5 saves with optional second-layer game-specific decryption
- **Encrypt** - Multi-step encrypt: decrypt on console, upload modified files, re-encrypt and resign
- **Re-region** - Change save region using a sample save's keystone
- **Create Save** - Build a new encrypted save from raw files + sce_sys
- **Convert** - Platform conversion for GTA V, RDR 2, BL 3, TTWL, Xenoblade 2
- **Quick Codes** - Apply Save Wizard hex quick codes to save files
- **Save DB** - Community save library where users contribute decrypted saves, vote on entries, and one-click encrypt to their account
- **Luac0re** - Quick-resign for Star Wars: Racer Revenge exploit saves (US/EU)

All PS4/PS5 operations communicate with a jailbroken console via the garlic-worker C agent.

## Requirements

- Python 3.12+
- A jailbroken PS4/PS5 running the [garlic-worker](https://github.com/earthonion/garlic-worker) agent

## Setup

1. **Create a virtual environment and install dependencies:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install quart aiofiles aioftp aiosqlite bcrypt python-dotenv \
    anycrc crc32c lz4 mmh3 orjson pillow pyaes pycryptodome \
    python-dateutil pyzstd zstandard
```

2. **Configure `.env`:**

```
IP=192.168.1.100          # PS4 IP address
FTP_PORT=2121             # PS4 FTP port
CECIE_PORT=1234           # PS4 Cecie socket port
UPLOAD_PATH=/mnt/usb0/    # PS4 remote upload directory
MOUNT_PATH=/mnt/sandbox/  # PS4 remote mount directory
SECRET_KEY=your-secret-key-here
DATABASE_PATH=htos_web.db
```

3. **Run:**

```bash
python app.py
```

The server starts at `http://localhost:5000`.

For production, use an ASGI server:

```bash
pip install hypercorn
hypercorn app:create_app() --bind 0.0.0.0:5000
```

## Usage

1. Register an account at `/register`
2. Log in and create a profile on the dashboard (PSN account ID in hex)
3. Select an operation from the nav bar
4. Upload save files (encrypted save pairs: `savename` + `savename.bin`)
5. Monitor job progress via real-time SSE log streaming
6. Download results as a zip when complete

### Encrypt (multi-step)

1. Upload encrypted save pairs - the server decrypts and mounts them on the PS4
2. The job pauses and shows the mounted file list
3. Upload your modified files through the second upload form
4. The server encrypts them back into the save, resigns, and provides the download

## Project Structure

```
HTOS-web/
  app.py              # Quart app factory + entry point
  auth.py             # Registration, login, session management (bcrypt)
  config.py           # Environment config
  models.py           # SQLite schema (users, profiles, jobs)
  routes/             # Blueprint route handlers
    main.py           # Dashboard, profile CRUD
    resign.py         # Resign operation
    decrypt.py        # Decrypt operation
    encrypt.py        # Encrypt operation (multi-step)
    reregion.py       # Re-region operation
    createsave.py     # Create save operation
    convert.py        # Platform conversion
    quickcodes.py     # Save Wizard quick codes
    savedb.py         # Community save database (browse, vote, contribute, encrypt)
    luac0re.py        # Quick-resign for Luac0re exploit saves
    jobs.py           # Job status, SSE stream, downloads
    admin_web.py      # Admin dashboard
    api.py            # Worker API endpoints
  services/
    jobs.py           # WebLogger, ServerSettings, Job, background task runner
    files.py          # File upload/download/zip helpers
  templates/          # Jinja2 HTML templates
  static/style.css    # Dark theme CSS
  app_core/           # Save processing logic (from HTOS)
  data/               # Game-specific crypto, converters, cheats (from HTOS)
  network/            # FTP + socket PS4 communication (from HTOS)
  utils/              # Constants, orbis save format, workspace helpers (from HTOS)
```

## Architecture

- **Quart** (async Flask) handles HTTP requests
- **SQLite + aiosqlite** stores users, profiles, jobs, and save DB entries
- **garlic-worker** C agents on PS4/PS5 poll the server for jobs and process saves
- **Server-Sent Events (SSE)** stream real-time logs from jobs to the browser
- **Community voting** on Save DB entries with Reddit-style auto-upvote and auto-delete at -10


