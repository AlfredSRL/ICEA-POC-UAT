"""Run bookkeeping: hashing and local, ignored run artifacts."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_run_dir(runtime_root: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runtime_root / "runs" / ts
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Timer:
    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.duration_seconds = time.monotonic() - self._start


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
