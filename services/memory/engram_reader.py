"""Engram observation reader for the pinard memory layer.

Pulls curated observations from Engram via its HTTP read API.

Spike-verified API surface (no /api prefix):
    GET /observations   — list observations (params: project, since_hours, limit)
    GET /search         — full-text search
    GET /health         — liveness
    GET /stats          — counts

Environment variables:
    ENGRAM_URL      — Engram HTTP API base URL (default: http://localhost:7783)
    ENGRAM_API_KEY  — Optional bearer token for Engram API auth
    ENGRAM_SINCE_HOURS — How many hours back to fetch observations (default: 168 = 7 days)

Usage::

    reader = EngramReader(group_id="genomics-build")
    observations = reader.fetch()
    for obs in observations:
        print(obs.session_id, obs.obs_type, obs.content)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

# Port 7783 is the spike-verified canonical deployment port (spike #43).
ENGRAM_URL = os.environ.get("ENGRAM_URL", "http://localhost:7783").rstrip("/")
ENGRAM_API_KEY = os.environ.get("ENGRAM_API_KEY", "")
ENGRAM_SINCE_HOURS = int(os.environ.get("ENGRAM_SINCE_HOURS", "168"))


@dataclass
class EngramObservation:
    """A single curated observation pulled from Engram."""

    obs_id: str
    session_id: str
    group_id: str
    obs_type: str          # e.g. "rule", "fact", "teaching-episode", "summary"
    content: str
    timestamp: datetime
    confidence: float = 1.0
    metadata: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class EngramReaderError(RuntimeError):
    pass


class EngramReader:
    """Reads curated observations from Engram for a given group_id."""

    def __init__(
        self,
        group_id: str,
        url: str = ENGRAM_URL,
        api_key: str = ENGRAM_API_KEY,
        since_hours: int = ENGRAM_SINCE_HOURS,
    ) -> None:
        self.group_id = group_id
        self._url = url
        self._since_hours = since_hours
        self._headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self) -> list[EngramObservation]:
        """Fetch curated observations for this group_id.

        Raises EngramReaderError on HTTP errors (4xx/5xx) — these indicate a
        configuration or endpoint bug and must surface loudly, not be swallowed.
        Transient connection errors (network down, timeout) also raise so the
        caller can decide whether to retry or skip.

        Callers that want non-fatal behaviour should catch EngramReaderError.
        The ingester loop does this and logs at ERROR level before continuing.
        """
        return list(self._fetch_via_api())

    # ── API fetch ─────────────────────────────────────────────────────────────

    def _fetch_via_api(self) -> Iterator[EngramObservation]:
        """Fetch observations from the Engram HTTP read API.

        Uses /observations (no /api prefix) — spike-verified surface.
        Raises EngramReaderError on any HTTP error so callers see config bugs.
        """
        params: dict[str, str | int] = {
            "project": self.group_id,
            "since_hours": self._since_hours,
            "limit": 500,
        }
        try:
            resp = httpx.get(
                f"{self._url}/observations",
                params=params,
                headers=self._headers,
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise EngramReaderError(f"Engram connection failed: {exc}") from exc

        if resp.status_code != 200:
            # 404 is NOT an empty result — live Engram returns 200+[] for unknown projects.
            # A 404 means the endpoint path is wrong (same class as the /api/observations bug).
            raise EngramReaderError(
                f"Engram API HTTP {resp.status_code} — possible endpoint misconfiguration: "
                f"{resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise EngramReaderError(f"Invalid JSON from Engram API: {exc}") from exc

        items = body if isinstance(body, list) else body.get("observations", [])
        for item in items:
            obs = self._parse_item(item)
            if obs is not None:
                yield obs

    def _parse_item(self, item: dict) -> EngramObservation | None:
        """Parse a single Engram API observation record."""
        try:
            raw_ts = item.get("created_at") or item.get("timestamp") or ""
            if raw_ts:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            else:
                ts = datetime.now(tz=timezone.utc)

            return EngramObservation(
                obs_id=str(item.get("id") or item.get("obs_id") or ""),
                session_id=str(item.get("session_id") or ""),
                group_id=str(item.get("project") or item.get("group_id") or self.group_id),
                obs_type=str(item.get("type") or item.get("obs_type") or "fact"),
                content=str(item.get("content") or item.get("body") or ""),
                timestamp=ts,
                confidence=float(item.get("confidence", 1.0)),
                metadata=dict(item.get("metadata") or {}),
            )
        except Exception as exc:
            logger.warning("Skipping malformed Engram observation: %s — %s", item, exc)
            return None
