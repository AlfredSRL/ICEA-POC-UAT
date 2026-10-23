"""HTTP client for POST /v1/extractions. One request per call, never retried."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import ApiSettings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiClient:
    def __init__(self, settings: ApiSettings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        # No retries, no redirects: a redirect could forward the Bearer token elsewhere.
        self._client = httpx.Client(
            base_url=settings.base_url, timeout=settings.timeout_seconds,
            follow_redirects=False, transport=transport,
            headers={"Authorization": f"Bearer {settings.token}"})

    @property
    def base_url(self) -> str:
        return self._settings.base_url

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def openapi(self) -> dict:
        response = self._client.get("/openapi.json")
        response.raise_for_status()
        return response.json()

    def extract(self, pdf_path: Path, filename: str, profile: str, option: str) -> dict:
        """Return a call record; transport failures become records, not exceptions."""
        record = {"started_at": now_iso(), "http_status": None, "body": None,
                  "error": None, "http_seconds": None}
        start = time.monotonic()
        try:
            with pdf_path.open("rb") as stream:
                response = self._client.post(
                    "/v1/extractions",
                    files={"file": (filename, stream, "application/pdf")},
                    data={"profile": profile, "extraction_option": option})
            record["http_status"] = response.status_code
            try:
                record["body"] = response.json()
            except ValueError:
                record["body"] = None
                record["error"] = "non_json_response"
            if response.status_code != 200 and record["error"] is None:
                body = record["body"] if isinstance(record["body"], dict) else {}
                record["error"] = str(body.get("error") or f"http_{response.status_code}")
        except httpx.TimeoutException:
            record["error"] = "timeout"
        except httpx.TransportError as error:
            record["error"] = f"network:{type(error).__name__}"
        finally:
            record["http_seconds"] = time.monotonic() - start
            record["finished_at"] = now_iso()
        return record
