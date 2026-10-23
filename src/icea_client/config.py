"""Local paths and API settings. Secrets come only from the environment (.env)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

# Each method is one opaque server extraction option. The option-to-model/strategy
# mapping is server-side configuration and must not be reproduced here.
METHODS = ("A", "B", "C", "D")
METHOD_NAMES = {method: f"Opzione {method}" for method in METHODS}


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).resolve() if value else default.resolve()


@dataclass(frozen=True)
class Paths:
    samples: Path
    ground_truth: Path
    runtime: Path

    @property
    def runs(self) -> Path:
        return self.runtime / "runs"

    @property
    def reports(self) -> Path:
        return self.runtime / "reports"

    @property
    def preflight(self) -> Path:
        return self.runtime / "preflight"


def paths() -> Paths:
    return Paths(samples=_path("ICEA_UAT_SAMPLES_DIR", ROOT / "data" / "uat" / "samples"),
                 ground_truth=_path("ICEA_UAT_GROUND_TRUTH_DIR", ROOT / "data" / "uat" / "ground_truth"),
                 runtime=_path("ICEA_UAT_RUNTIME_DIR", ROOT / "runtime"))


def resolve_ground_truth(value: str | Path, base: Paths) -> Path:
    """The workbook name is free, but the file must sit in the ignored GT directory."""
    candidate = Path(value)
    if not candidate.is_absolute() and not candidate.exists():
        candidate = base.ground_truth / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(base.ground_truth):
        raise ValueError(f"ground truth workbook must be inside {base.ground_truth}")
    if not candidate.is_file():
        raise FileNotFoundError(f"ground truth workbook not found: {candidate.name}")
    return candidate


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    token: str
    timeout_seconds: float


def plain_http_remote(url: str) -> bool:
    """True when the token would travel unencrypted beyond loopback (warning only)."""
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname not in LOCAL_HTTP_HOSTS


def check_api_url(url: str) -> str:
    """The client is transport-agnostic: the URL in .env alone selects local or remote."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("ICEA_API_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ICEA_API_URL must not carry credentials, query or fragment")
    return url.rstrip("/")


def api_settings() -> ApiSettings:
    url = os.environ.get("ICEA_API_URL", "").strip()
    token = os.environ.get("ICEA_API_TOKEN", "").strip()
    if not url or not token:
        raise ValueError("ICEA_API_URL and ICEA_API_TOKEN must be set in the local .env")
    timeout = float(os.environ.get("ICEA_API_TIMEOUT_SECONDS", "600"))
    if timeout <= 0:
        raise ValueError("ICEA_API_TIMEOUT_SECONDS must be positive")
    url = check_api_url(url)
    if plain_http_remote(url):
        print(f"AVVISO: {urlparse(url).hostname} via http, token non cifrato in transito. "
              "Accettabile solo su rete Docker o privata.", file=sys.stderr)
    return ApiSettings(url, token, timeout)
