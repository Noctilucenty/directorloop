"""Identifiers, timestamps and content hashing shared by every domain record."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def new_id(prefix: str) -> str:
    """Time-ordered, unguessable id such as `ver_18f3a9c2b1_7e2f9a3c`."""
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(4)}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def short_hash(full: str, n: int = 12) -> str:
    return full[:n]
