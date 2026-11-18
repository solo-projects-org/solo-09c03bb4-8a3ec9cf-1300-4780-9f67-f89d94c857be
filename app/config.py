"""Runtime configuration.

Everything can be overridden through environment variables so the same image
runs both under Docker Compose (persistent volume on /data) and in the test
suite (temporary directory).
"""
from __future__ import annotations

import os
from pathlib import Path


def _resolve(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = _resolve(os.environ.get("DEID_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("DEID_DB_PATH", DATA_DIR / "app.db"))
UPLOAD_DIR = _resolve(os.environ.get("DEID_UPLOAD_DIR", DATA_DIR / "uploads"))
PACKAGE_DIR = _resolve(os.environ.get("DEID_PACKAGE_DIR", DATA_DIR / "packages"))

# Background worker polling interval (seconds).
WORKER_INTERVAL = float(os.environ.get("DEID_WORKER_INTERVAL", "1.0"))
# Reject uploads larger than this (bytes).
MAX_UPLOAD_BYTES = int(os.environ.get("DEID_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

# Validated identifier pattern: prevents path traversal / header injection.
_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def is_valid_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and all(ch in _ID_ALLOWED for ch in value)
        and value not in (".", "..")
    )
