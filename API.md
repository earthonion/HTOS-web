# HTOS REST API

Base URL: `https://garlicsaves.com/api/v1`

## Authentication

All requests require a worker key passed via the `X-Worker-Key` header. Generate a key from the [Contribute](https://garlicsaves.com/contribute) page.

```bash
-H "X-Worker-Key: YOUR_KEY_HERE"
```

## Endpoints

### Create Job

`POST /api/v1/jobs`

Submit a job with files via multipart form upload.

**Common fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `operation` | string | yes | `encrypt`, `decrypt`, `resign`, `reregion`, or `createsave` |
| `files` | file(s) | yes | Save files or a zip archive |

**Operation-specific fields:**

| Field | Operations | Required | Description |
|-------|-----------|----------|-------------|
| `account_id` | encrypt, resign, reregion, createsave | yes | Hex account ID (e.g. `0000000000000000`) |
| `include_sce_sys` | decrypt | no | `true` to include sce_sys in output (default: false) |
| `savename` | createsave | yes | Save directory name |
| `saveblocks` | createsave | yes | Number of save blocks |
| `sample` | reregion | yes | Sample save file(s) for keystone extraction |

#### Encrypt

Encrypt decrypted save files into a PS4-mountable save. Upload a zip containing `sce_sys/param.sfo` and save data files. The save name and block count are automatically derived from `param.sfo`.

```bash
curl -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY" \
  -F "operation=encrypt" \
  -F "account_id=0000000000000000" \
  -F "files=@decrypted_save.zip"
```

```json
{"job_id": "fcca3c78-...", "status": "queued"}
```

#### Decrypt

Decrypt PS4 save files. Upload save + sealed key (.bin) pairs.

```bash
curl -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY" \
  -F "operation=decrypt" \
  -F "files=@savefile" \
  -F "files=@savefile.bin"
```

#### Resign

Re-sign saves to a different account. Upload save + sealed key (.bin) pairs.

```bash
curl -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY" \
  -F "operation=resign" \
  -F "account_id=1234567890abcdef" \
  -F "files=@savefile" \
  -F "files=@savefile.bin"
```

#### Re-region

Re-region saves using a sample save's keystone. Upload target saves in the `files` field and a sample save pair in the `sample` field.

```bash
curl -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY" \
  -F "operation=reregion" \
  -F "account_id=0000000000000000" \
  -F "files=@target_save" \
  -F "files=@target_save.bin" \
  -F "sample=@sample_save" \
  -F "sample=@sample_save.bin"
```

#### Create Save

Create a new encrypted save from raw files. Requires explicit `savename` and `saveblocks`.

```bash
curl -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY" \
  -F "operation=createsave" \
  -F "account_id=0000000000000000" \
  -F "savename=mysave" \
  -F "saveblocks=96" \
  -F "files=@save_contents.zip"
```

---

### Get Job Status

`GET /api/v1/jobs/<job_id>`

Returns the job status and log messages.

```bash
curl https://garlicsaves.com/api/v1/jobs/fcca3c78-c4d7-41c4-b51e-d15760c54527 \
  -H "X-Worker-Key: YOUR_KEY"
```

```json
{
  "id": "fcca3c78-...",
  "operation": "encrypt",
  "status": "done",
  "created_at": "2026-02-24 05:18:11",
  "error": null,
  "logs": [
    {"level": "INFO", "msg": "Starting encrypt...", "time": "..."},
    {"level": "INFO", "msg": "Console max keyset: 10", "time": "..."},
    {"level": "INFO", "msg": "Encrypted localstorage.aes", "time": "..."},
    {"level": "INFO", "msg": "Done! Your files are ready for download.", "time": "..."}
  ]
}
```

**Status values:** `queued`, `running`, `done`, `failed`

---

### Download Result

`GET /api/v1/jobs/<job_id>/result`

Download the result zip file. Only available when status is `done`.

```bash
curl -o result.zip https://garlicsaves.com/api/v1/jobs/fcca3c78-c4d7-41c4-b51e-d15760c54527/result \
  -H "X-Worker-Key: YOUR_KEY"
```

---

### List Jobs

`GET /api/v1/jobs`

List your recent jobs (up to 50).

```bash
curl https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: YOUR_KEY"
```

```json
[
  {"id": "fcca3c78-...", "operation": "encrypt", "status": "done", "created_at": "..."},
  {"id": "a1b2c3d4-...", "operation": "decrypt", "status": "queued", "created_at": "..."}
]
```

---

## Errors

All errors return JSON with an `error` field:

```json
{"error": "Missing required field: account_id"}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request (missing fields, invalid params) |
| 401 | Invalid or missing worker key |
| 404 | Job not found |
| 500 | Server error |

## Typical Workflow

```bash
KEY="YOUR_WORKER_KEY"

# 1. Submit job
JOB_ID=$(curl -s -X POST https://garlicsaves.com/api/v1/jobs \
  -H "X-Worker-Key: $KEY" \
  -F "operation=encrypt" \
  -F "account_id=0000000000000000" \
  -F "files=@save.zip" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "Job: $JOB_ID"

# 2. Poll until done
while true; do
  STATUS=$(curl -s "https://garlicsaves.com/api/v1/jobs/$JOB_ID" \
    -H "X-Worker-Key: $KEY" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Status: $STATUS"
  [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ] && break
  sleep 5
done

# 3. Download result
curl -o result.zip "https://garlicsaves.com/api/v1/jobs/$JOB_ID/result" \
  -H "X-Worker-Key: $KEY"
```
