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
- A jailbroken PS4/PS5 running the [garlic-worker](https://git.etawen.dev/earthonion/garlic-worker) agent

## Setup

1. **Create a virtual environment and install dependencies:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

## Worker updates

HTOS enforces a minimum worker version from `MIN_WORKER_VERSION` in `routes/api.py`. Older workers receive HTTP 426 from `/api/worker/next`, then request a replacement from `/api/worker/update`. API job-submission toggles do not disable worker polling or updates.

The update endpoint proxies the **latest GitHub release** from `earthonion/garlic-worker` and caches each ELF for five minutes under `/var/cache/htos/worker-update/`. The required asset names are:

- `garlic-worker-ps4.elf`
- `garlic-worker-ps5.elf`

### Publishing a worker update

1. Update the version in the worker source and build both platform ELFs. Check the embedded version with `strings` before publishing.
2. Publish a non-prerelease GitHub release containing both assets with the exact names above. GitHub's `releases/latest/download` URL must serve this release.
3. If the server minimum changes, update `MIN_WORKER_VERSION` in `routes/api.py` and the outdated-worker check in `templates/admin.html` to the same version. Do not raise the minimum before the matching assets are downloadable.
4. Deploy HTOS. To force an immediate refresh instead of waiting for the five-minute cache, remove the cached ELFs:

   ```bash
   rm -f /var/cache/htos/worker-update/garlic-worker-ps4.elf
   rm -f /var/cache/htos/worker-update/garlic-worker-ps5.elf
   ```

5. Verify both update endpoints using a valid worker key, replacing `1.5.9` with the intended release:

   ```bash
   curl -fSLo /tmp/garlic-worker-ps5.elf \
     -H "X-Worker-Key: YOUR_KEY" \
     "https://garlicsaves.com/api/worker/update?platform=ps5"
   curl -fSLo /tmp/garlic-worker-ps4.elf \
     -H "X-Worker-Key: YOUR_KEY" \
     "https://garlicsaves.com/api/worker/update?platform=ps4"
   strings /tmp/garlic-worker-ps5.elf | grep '1.5.9'
   strings /tmp/garlic-worker-ps4.elf | grep '1.5.9'
   ```

6. Watch online worker user agents and the job queue. Outdated instances should download the new ELF, restart, and stop receiving 426. No HTOS restart is required after only clearing the update cache.

### Avoiding an update loop

A mirror whose latest release is older than `MIN_WORKER_VERSION` creates a self-reinforcing loop: workers update, still receive 426, and repeatedly poll `/next` and `/update`. This can look like a DDoS and prevent jobs from draining. Check the asset's actual version, not only its release tag, and confirm `/update` returns HTTP 200 rather than 499 or 502.

Stale recovery requeues jobs when an assigned instance has not heartbeated for five minutes. Running jobs with no instance assignment are requeued after 30 minutes. Redundant worker `last_used` timestamp writes are throttled to reduce SQLite lock contention during update bursts.


