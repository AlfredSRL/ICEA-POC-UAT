"""Vendored server modules. They use flat imports, so their directory joins sys.path."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CANONICAL_DIR = Path(__file__).resolve().parent
if str(CANONICAL_DIR) not in sys.path:
    sys.path.insert(0, str(CANONICAL_DIR))


def verify_manifest() -> list[str]:
    """Return the vendored files whose bytes differ from MANIFEST.json."""
    manifest = json.loads((CANONICAL_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    drift = []
    for name, expected in manifest["files"].items():
        path = CANONICAL_DIR / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual != expected:
            drift.append(name)
    return drift
