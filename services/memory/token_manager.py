"""LLM token manager for the pinard memory layer.

Wraps :class:`~services.memory.llm_client.LLMClient` with an escalating-backoff
polling cycle. Probe/token-fetch errors are surfaced as :class:`LLMUnavailable`.

Environment variables (all resolved via llm_client):
    MEMORY_LLM_API          — protocol adapter (default: ``anthropic-messages``)
    MEMORY_LLM_BASE_URL     — endpoint override
    MEMORY_LLM_MODEL        — model id
    MEMORY_LLM_AUTH         — token source (``google-sa`` | ``url`` | ``static-key``)
    MEMORY_TOKEN_URL        — pour-token URL (used when MEMORY_LLM_AUTH=url or auto)
    ANTHROPIC_API_KEY       — direct Anthropic key (MEMORY_LLM_AUTH=static-key)
    GOOGLE_APPLICATION_CREDENTIALS — SA JSON path (MEMORY_LLM_AUTH=google-sa)

Usage::

    manager = TokenManager()
    try:
        client = manager.get_client()   # raises LLMUnavailable if unavailable
    except LLMUnavailable:
        pass
"""
from __future__ import annotations

import logging
import time

from .llm_client import LLMAuthError, LLMError, LLMClient, build_llm_client

logger = logging.getLogger(__name__)

# Escalating backoff delays (seconds) for repeated LLM-unavailable probes.
BACKOFF_SCHEDULE = [5 * 60, 10 * 60, 30 * 60]  # 5m, 10m, 30m


class LLMUnavailable(Exception):
    """Raised when the LLM endpoint is unreachable or the token is expired."""


class TokenManager:
    """Manages LLM availability with probe + escalating backoff."""

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._llm_client: LLMClient = llm_client or build_llm_client()
        self._probe_failures: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def get_client(self) -> LLMClient:
        """Return a probed LLMClient, or raise LLMUnavailable.

        Probes the endpoint for liveness. Does NOT implement the retry loop —
        callers manage the sleep cycle.
        """
        try:
            self._llm_client.probe()
        except LLMAuthError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except LLMError as exc:
            raise LLMUnavailable(str(exc)) from exc
        self._probe_failures = 0
        return self._llm_client

    def backoff_delay(self) -> float:
        """Return the next backoff delay in seconds and increment the failure counter."""
        idx = min(self._probe_failures, len(BACKOFF_SCHEDULE) - 1)
        delay = BACKOFF_SCHEDULE[idx]
        self._probe_failures += 1
        return delay

    def reset_failures(self) -> None:
        self._probe_failures = 0

    def poll_until_available(self) -> LLMClient:
        """Block until the LLM becomes available, using escalating backoff.

        Returns a probed LLMClient.
        """
        while True:
            try:
                client = self.get_client()
                logger.info("LLM probe succeeded — transitioning to draining mode")
                return client
            except LLMUnavailable as exc:
                delay = self.backoff_delay()
                logger.warning(
                    "LLM probe failed (%s); retrying in %.0fs", exc, delay
                )
                time.sleep(delay)
