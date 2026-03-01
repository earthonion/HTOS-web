import os
from dotenv import load_dotenv

load_dotenv()

# Worker authentication
WORKER_KEY = os.getenv("WORKER_KEY", "")
WORKER_SIGNING_KEY = os.getenv("WORKER_SIGNING_KEY", "")
PS5_WORKERS_ENABLED = os.getenv("PS5_WORKERS_ENABLED", "0").lower() in ("1", "true", "yes")

# Web server
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
DATABASE_PATH = os.getenv("DATABASE_PATH", "htos_web.db")
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(2 * 1024 * 1024 * 1024)))  # 2GB default
MAX_SAVE_FILE_SIZE = int(os.getenv("MAX_SAVE_FILE_SIZE", str(64 * 1024 * 1024)))  # 64MB default per file

# Chunked uploads
CHUNK_DIR = os.path.join("workspace", "chunks")
CHUNK_SIZE = 50 * 1024 * 1024  # 50MB
CHUNK_EXPIRY = 3600  # 1 hour TTL for incomplete uploads

# Paths
UPLOAD_DIR = os.path.join("workspace", "uploads")
RESULT_DIR = os.path.join("workspace", "results")
WORKSPACE_DIR = os.path.join("workspace", "processing")

for d in [UPLOAD_DIR, RESULT_DIR, WORKSPACE_DIR, CHUNK_DIR]:
    os.makedirs(d, exist_ok=True)
