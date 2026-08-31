"""Model-agnostic LLM client for the pinard memory service.

Config-driven via environment variables — swap provider by changing env, no code change.

Environment variables:
    MEMORY_LLM_API       — protocol adapter: ``openai-chat`` | ``anthropic-messages``
                           (default: ``anthropic-messages``)
    MEMORY_LLM_BASE_URL  — endpoint override (e.g. Vertex OpenAI-compat URL,
                           ``https://llm-proxy.example.com``, or local)
    MEMORY_LLM_MODEL     — model id (e.g. ``gemini-2.0-flash``, ``claude-haiku-4-5-20251001``)
    MEMORY_LLM_AUTH      — token source: ``google-sa`` | ``url`` | ``static-key``
                           (default: ``url`` when MEMORY_TOKEN_URL is set, else ``static-key``)

    # Auth-source-specific:
    GOOGLE_APPLICATION_CREDENTIALS — path to SA JSON (for ``google-sa``)
    MEMORY_TOKEN_URL                — pour-token URL (for ``url``)
    ANTHROPIC_API_KEY / OPENAI_API_KEY — static key (for ``static-key``)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_API = "anthropic-messages"
_DEFAULT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
_DEFAULT_MODEL_OPENAI = "gemini-2.0-flash"

MEMORY_LLM_API = os.environ.get("MEMORY_LLM_API", _DEFAULT_API)
MEMORY_LLM_BASE_URL = os.environ.get("MEMORY_LLM_BASE_URL", "")
MEMORY_LLM_MODEL = os.environ.get("MEMORY_LLM_MODEL", "")
MEMORY_LLM_AUTH = os.environ.get("MEMORY_LLM_AUTH", "")

# ── Exceptions ────────────────────────────────────────────────────────────────


class LLMAuthError(Exception):
    """Raised when the LLM auth token is missing, expired, or rejected (401/403).

    Signals the caller to enter the polling-backoff cycle — equivalent to the
    previous anthropic.AuthenticationError path.
    """


class LLMError(Exception):
    """Raised on non-auth LLM errors (network, 5xx, parse failures)."""


# ── Token providers ───────────────────────────────────────────────────────────


class _GoogleSATokenProvider:
    """Mint OAuth access tokens from a Google service-account JSON key.

    Tokens are cached and refreshed ~5 minutes before expiry.
    """

    def __init__(self, sa_path: str) -> None:
        self._sa_path = sa_path
        self._token: str = ""
        self._expiry: float = 0.0

    def get_token(self) -> str:
        if self._token and time.monotonic() < self._expiry - 300:
            return self._token
        try:
            from google.oauth2 import service_account  # type: ignore[import]
            from google.auth.transport.requests import Request  # type: ignore[import]

            creds = service_account.Credentials.from_service_account_file(
                self._sa_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            creds.refresh(Request())
            self._token = creds.token  # type: ignore[assignment]
            # google-auth expiry is a datetime; convert to monotonic-relative seconds.
            import datetime
            if creds.expiry:
                remaining = (creds.expiry - datetime.datetime.utcnow()).total_seconds()
                self._expiry = time.monotonic() + max(remaining, 0)
            else:
                self._expiry = time.monotonic() + 3600
        except Exception as exc:
            raise LLMAuthError(f"google-sa token mint failed: {exc}") from exc
        return self._token


class _URLTokenProvider:
    """Fetch a token from a URL (pour-token pattern)."""

    def __init__(self, url: str) -> None:
        self._url = url

    def get_token(self) -> str:
        try:
            resp = httpx.get(self._url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            raise LLMAuthError(f"Token URL fetch failed: {exc}") from exc
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        key = body.get("api_key") or body.get("token") or resp.text.strip()
        if not key:
            raise LLMAuthError("Token URL returned an empty key")
        return key


class _StaticKeyProvider:
    """Return a static API key from an env var or literal."""

    def __init__(self, key: str) -> None:
        self._key = key

    def get_token(self) -> str:
        if not self._key:
            raise LLMAuthError(
                "No static API key configured (set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "or MEMORY_TOKEN_URL)"
            )
        return self._key


# ── LLMClient ─────────────────────────────────────────────────────────────────


class LLMClient:
    """Thin model-agnostic LLM client.

    Instantiate via :func:`build_llm_client` (reads env) or directly.

    Attributes:
        api: ``"openai-chat"`` | ``"anthropic-messages"``
        model: model id string
        base_url: endpoint override (empty = SDK default)
    """

    def __init__(
        self,
        api: str,
        model: str,
        token_provider: Any,
        base_url: str = "",
    ) -> None:
        self.api = api
        self.model = model
        self.base_url = base_url
        self._token_provider = token_provider

    # ── Token / key ───────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        return self._token_provider.get_token()

    # ── Probe ─────────────────────────────────────────────────────────────────

    def probe(self) -> None:
        """Probe the endpoint with a minimal request.

        Raises:
            LLMAuthError: on 401/403 or missing token.
            LLMError: on network / other errors.
        """
        token = self._get_token()
        if self.api == "anthropic-messages":
            self._probe_anthropic(token)
        else:
            self._probe_openai(token)

    def _probe_anthropic(self, token: str) -> None:
        base = self.base_url or "https://api.anthropic.com"
        try:
            resp = httpx.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": token,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
        except httpx.RequestError as exc:
            raise LLMError(f"Anthropic API unreachable: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"LLM token rejected (HTTP {resp.status_code})")

    def _probe_openai(self, token: str) -> None:
        base = self.base_url or "https://api.openai.com"
        try:
            resp = httpx.post(
                f"{base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
        except httpx.RequestError as exc:
            raise LLMError(f"OpenAI-compat API unreachable: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"LLM token rejected (HTTP {resp.status_code})")

    # ── Completion ────────────────────────────────────────────────────────────

    def complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        system: str | None = None,
    ) -> str:
        """Send a chat completion request and return the response text.

        Args:
            messages: list of ``{"role": ..., "content": ...}`` dicts.
            max_tokens: maximum tokens in the response.
            system: optional system prompt (anthropic-messages only; for
                openai-chat it is prepended as a ``system`` role message).

        Returns:
            The assistant's response text.

        Raises:
            LLMAuthError: on 401/403.
            LLMError: on other failures.
        """
        token = self._get_token()
        if self.api == "anthropic-messages":
            return self._complete_anthropic(token, messages, max_tokens, system)
        return self._complete_openai(token, messages, max_tokens, system)

    def _complete_anthropic(
        self,
        token: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        system: str | None,
    ) -> str:
        try:
            import anthropic  # type: ignore[import]
        except ImportError as exc:
            raise LLMError("anthropic package not installed") from exc

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        client_kwargs: dict[str, Any] = {"api_key": token}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url

        try:
            client = anthropic.Anthropic(**client_kwargs)
            response = client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(str(exc)) from exc
        except Exception as exc:
            raise LLMError(f"Anthropic completion failed: {exc}") from exc

        text = response.content[0].text if response.content else ""
        return text

    def _complete_openai(
        self,
        token: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        system: str | None,
    ) -> str:
        try:
            from openai import OpenAI, AuthenticationError  # type: ignore[import]
        except ImportError as exc:
            raise LLMError("openai package not installed") from exc

        all_messages = list(messages)
        if system:
            all_messages = [{"role": "system", "content": system}] + all_messages

        client_kwargs: dict[str, Any] = {"api_key": token}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url

        try:
            client = OpenAI(**client_kwargs)
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=all_messages,  # type: ignore[arg-type]
            )
        except AuthenticationError as exc:
            raise LLMAuthError(str(exc)) from exc
        except Exception as exc:
            raise LLMError(f"OpenAI-compat completion failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        return choice.message.content or "" if choice else ""

    # ── Graphiti client factory ───────────────────────────────────────────────

    def build_graphiti_llm_client(self) -> Any:
        """Return a graphiti_core LLM client driven by this LLMClient's config.

        Raises LLMError if graphiti_core is not installed.
        """
        token = self._get_token()
        try:
            if self.api == "anthropic-messages":
                from graphiti_core.llm_client.anthropic_client import AnthropicClient  # type: ignore[import]
                from graphiti_core.llm_client.config import LLMConfig  # type: ignore[import]

                cfg = LLMConfig(api_key=token, model=self.model)
                return AnthropicClient(config=cfg)
            else:
                from graphiti_core.llm_client.openai_client import OpenAIClient  # type: ignore[import]
                from graphiti_core.llm_client.config import LLMConfig  # type: ignore[import]

                kwargs: dict[str, Any] = {"api_key": token, "model": self.model}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                cfg = LLMConfig(**kwargs)
                return OpenAIClient(config=cfg)
        except ImportError as exc:
            raise LLMError("graphiti_core not installed") from exc


# ── Factory ───────────────────────────────────────────────────────────────────


def _resolve_auth(api: str, auth_override: str | None = None) -> Any:
    """Resolve the token provider from env (read dynamically)."""
    auth = auth_override if auth_override is not None else os.environ.get("MEMORY_LLM_AUTH", "")

    if auth == "google-sa":
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not sa_path:
            raise ValueError(
                "MEMORY_LLM_AUTH=google-sa requires GOOGLE_APPLICATION_CREDENTIALS"
            )
        return _GoogleSATokenProvider(sa_path)

    if auth == "url":
        url = os.environ.get("MEMORY_TOKEN_URL", "")
        if not url:
            raise ValueError("MEMORY_LLM_AUTH=url requires MEMORY_TOKEN_URL")
        return _URLTokenProvider(url)

    if auth == "static-key":
        key = (
            os.environ.get("ANTHROPIC_API_KEY", "")
            if api == "anthropic-messages"
            else os.environ.get("OPENAI_API_KEY", "")
        )
        return _StaticKeyProvider(key)

    # Auto-detect: prefer url if MEMORY_TOKEN_URL set, fall back to static key.
    token_url = os.environ.get("MEMORY_TOKEN_URL", "")
    if token_url:
        return _URLTokenProvider(token_url)

    key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        if api == "anthropic-messages"
        else os.environ.get("OPENAI_API_KEY", "")
    )
    return _StaticKeyProvider(key)


def build_llm_client(
    api: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    auth: str | None = None,
) -> LLMClient:
    """Build an LLMClient from env (or explicit overrides).

    All env vars are read dynamically at call time (not at import time).

    Args:
        api: ``"openai-chat"`` | ``"anthropic-messages"``. Defaults to
            ``MEMORY_LLM_API`` env var (``"anthropic-messages"``).
        model: model id. Defaults to ``MEMORY_LLM_MODEL`` env var, then a
            per-api default.
        base_url: endpoint override. Defaults to ``MEMORY_LLM_BASE_URL``.
        auth: auth provider name. Defaults to ``MEMORY_LLM_AUTH`` auto-detect.
    """
    # Read env dynamically so tests can monkeypatch.
    env_api = os.environ.get("MEMORY_LLM_API", _DEFAULT_API)
    env_model = os.environ.get("MEMORY_LLM_MODEL", "")
    env_base = os.environ.get("MEMORY_LLM_BASE_URL", "")

    resolved_api = api or env_api or _DEFAULT_API
    resolved_base = base_url if base_url is not None else env_base

    if model:
        resolved_model = model
    elif env_model:
        resolved_model = env_model
    else:
        resolved_model = (
            _DEFAULT_MODEL_ANTHROPIC
            if resolved_api == "anthropic-messages"
            else _DEFAULT_MODEL_OPENAI
        )

    provider = _resolve_auth(resolved_api, auth_override=auth)

    return LLMClient(
        api=resolved_api,
        model=resolved_model,
        token_provider=provider,
        base_url=resolved_base,
    )
