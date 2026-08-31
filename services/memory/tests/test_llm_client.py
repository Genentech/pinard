"""Unit tests for the model-agnostic LLMClient abstraction.

Tests:
1. Auth providers: google-sa, url, static-key
2. Probe: anthropic-messages and openai-chat adapters
3. complete(): both adapters (mocked SDKs)
4. Token refresh (google-sa cached + expired)
5. LLMAuthError raised on 401/403
6. LLMError raised on network failures
7. build_llm_client() factory reads env vars
8. build_graphiti_llm_client() selects correct graphiti adapter
9. TokenManager.get_client() wraps probe errors as LLMUnavailable
10. TokenManager.backoff_delay() follows schedule
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.llm_client import (
    LLMAuthError,
    LLMClient,
    LLMError,
    _GoogleSATokenProvider,
    _StaticKeyProvider,
    _URLTokenProvider,
    build_llm_client,
)
from services.memory.token_manager import LLMUnavailable, TokenManager


# ── 1. Auth providers ─────────────────────────────────────────────────────────


class TestStaticKeyProvider:
    def test_returns_key(self) -> None:
        p = _StaticKeyProvider("my-key")
        assert p.get_token() == "my-key"

    def test_raises_on_empty_key(self) -> None:
        p = _StaticKeyProvider("")
        with pytest.raises(LLMAuthError, match="No static API key"):
            p.get_token()


class TestURLTokenProvider:
    def test_fetches_json_api_key(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"api_key": "fetched-key"}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            p = _URLTokenProvider("http://token-server/key")
            assert p.get_token() == "fetched-key"

    def test_fetches_json_token_field(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"token": "tok-abc"}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            p = _URLTokenProvider("http://token-server/key")
            assert p.get_token() == "tok-abc"

    def test_fetches_plain_text(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.text = "sk-plain-key"
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            p = _URLTokenProvider("http://token-server/key")
            assert p.get_token() == "sk-plain-key"

    def test_raises_llm_auth_error_on_request_failure(self) -> None:
        import httpx
        with patch("httpx.get", side_effect=httpx.RequestError("connection refused")):
            p = _URLTokenProvider("http://bad-server/key")
            with pytest.raises(LLMAuthError, match="Token URL fetch failed"):
                p.get_token()

    def test_raises_llm_auth_error_on_empty_response(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {}
        mock_resp.text = ""
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            p = _URLTokenProvider("http://token-server/key")
            with pytest.raises(LLMAuthError, match="empty key"):
                p.get_token()


class TestGoogleSATokenProvider:
    def _make_creds(self, token: str = "gsa-token", expiry_seconds: int = 3600) -> MagicMock:
        import datetime
        creds = MagicMock()
        creds.token = token
        creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=expiry_seconds)
        return creds

    def test_mints_token_from_sa_file(self) -> None:
        creds = self._make_creds("gsa-tok")
        with (
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                return_value=creds,
            ),
            patch("google.auth.transport.requests.Request"),
        ):
            p = _GoogleSATokenProvider("/path/to/sa.json")
            token = p.get_token()
        assert token == "gsa-tok"

    def test_caches_token_within_expiry(self) -> None:
        creds = self._make_creds("gsa-cached")
        with (
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                return_value=creds,
            ),
            patch("google.auth.transport.requests.Request"),
        ):
            p = _GoogleSATokenProvider("/path/to/sa.json")
            t1 = p.get_token()
            # Second call should NOT re-call from_service_account_file.
            t2 = p.get_token()
        assert t1 == t2 == "gsa-cached"

    def test_refreshes_expired_token(self) -> None:
        creds1 = self._make_creds("first-token", expiry_seconds=200)
        creds2 = self._make_creds("second-token", expiry_seconds=3600)
        call_count = {"n": 0}

        def _from_file(path, scopes=None):
            call_count["n"] += 1
            return creds1 if call_count["n"] == 1 else creds2

        with (
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                side_effect=_from_file,
            ),
            patch("google.auth.transport.requests.Request"),
        ):
            p = _GoogleSATokenProvider("/path/to/sa.json")
            # First call — populates cache with first-token.
            first = p.get_token()
            assert first == "first-token"
            # Manually expire the cache.
            p._expiry = time.monotonic() - 1
            # Second call — should re-mint and return second-token.
            token = p.get_token()
        assert token == "second-token"

    def test_raises_llm_auth_error_on_failure(self) -> None:
        with patch(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            side_effect=FileNotFoundError("no such file"),
        ):
            p = _GoogleSATokenProvider("/nonexistent/sa.json")
            with pytest.raises(LLMAuthError, match="google-sa token mint failed"):
                p.get_token()


# ── 2 & 3. Probe + complete() ─────────────────────────────────────────────────


def _make_anthropic_client(model: str = "claude-haiku") -> LLMClient:
    return LLMClient(
        api="anthropic-messages",
        model=model,
        token_provider=_StaticKeyProvider("test-key"),
    )


def _make_openai_client(model: str = "gemini-2.0-flash") -> LLMClient:
    return LLMClient(
        api="openai-chat",
        model=model,
        token_provider=_StaticKeyProvider("test-key"),
    )


class TestProbeAnthropic:
    def test_probe_ok_on_non_auth_status(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        with patch("httpx.post", return_value=mock_resp):
            _make_anthropic_client().probe()  # should not raise

    def test_probe_raises_llm_auth_error_on_401(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(LLMAuthError):
                _make_anthropic_client().probe()

    def test_probe_raises_llm_auth_error_on_403(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(LLMAuthError):
                _make_anthropic_client().probe()

    def test_probe_raises_llm_error_on_request_error(self) -> None:
        import httpx
        with patch("httpx.post", side_effect=httpx.RequestError("timeout")):
            with pytest.raises(LLMError, match="unreachable"):
                _make_anthropic_client().probe()

    def test_probe_uses_custom_base_url(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        captured: list[str] = []
        original_post = httpx.post

        def _post(url: str, **kwargs: Any) -> Any:
            captured.append(url)
            return mock_resp

        with patch("httpx.post", side_effect=_post):
            client = LLMClient(
                api="anthropic-messages",
                model="claude-haiku",
                token_provider=_StaticKeyProvider("key"),
                base_url="https://custom.endpoint.com",
            )
            client.probe()
        assert captured[0].startswith("https://custom.endpoint.com")


class TestProbeOpenAI:
    def test_probe_ok_on_non_auth_status(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        with patch("httpx.post", return_value=mock_resp):
            _make_openai_client().probe()

    def test_probe_raises_llm_auth_error_on_401(self) -> None:
        import httpx
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(LLMAuthError):
                _make_openai_client().probe()

    def test_probe_raises_llm_error_on_request_error(self) -> None:
        import httpx
        with patch("httpx.post", side_effect=httpx.RequestError("refused")):
            with pytest.raises(LLMError):
                _make_openai_client().probe()


class TestCompleteAnthropic:
    def _mock_anthropic(self, text: str) -> MagicMock:
        content_block = MagicMock()
        content_block.text = text
        message = MagicMock()
        message.content = [content_block]
        client_mock = MagicMock()
        client_mock.messages.create.return_value = message
        anthropic_mod = MagicMock()
        anthropic_mod.Anthropic.return_value = client_mock
        return anthropic_mod

    def test_returns_response_text(self) -> None:
        anthropic_mod = self._mock_anthropic("Hello world")
        with patch.dict("sys.modules", {"anthropic": anthropic_mod}):
            result = _make_anthropic_client().complete(
                messages=[{"role": "user", "content": "hi"}]
            )
        assert result == "Hello world"

    def test_passes_system_prompt(self) -> None:
        anthropic_mod = self._mock_anthropic("ok")
        with patch.dict("sys.modules", {"anthropic": anthropic_mod}):
            _make_anthropic_client().complete(
                messages=[{"role": "user", "content": "hi"}],
                system="Be concise.",
            )
        call_kwargs = anthropic_mod.Anthropic.return_value.messages.create.call_args.kwargs
        assert call_kwargs.get("system") == "Be concise."

    def test_raises_llm_auth_error_on_authentication_error(self) -> None:
        anthropic_mod = MagicMock()
        anthropic_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        anthropic_mod.Anthropic.return_value.messages.create.side_effect = (
            anthropic_mod.AuthenticationError("expired")
        )
        with patch.dict("sys.modules", {"anthropic": anthropic_mod}):
            with pytest.raises(LLMAuthError):
                _make_anthropic_client().complete(
                    messages=[{"role": "user", "content": "hi"}]
                )

    def test_raises_llm_error_on_other_exception(self) -> None:
        anthropic_mod = MagicMock()
        anthropic_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        anthropic_mod.Anthropic.return_value.messages.create.side_effect = RuntimeError("timeout")
        with patch.dict("sys.modules", {"anthropic": anthropic_mod}):
            with pytest.raises(LLMError):
                _make_anthropic_client().complete(
                    messages=[{"role": "user", "content": "hi"}]
                )

    def test_empty_content_returns_empty_string(self) -> None:
        message = MagicMock()
        message.content = []
        client_mock = MagicMock()
        client_mock.messages.create.return_value = message
        anthropic_mod = MagicMock()
        anthropic_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        anthropic_mod.Anthropic.return_value = client_mock
        with patch.dict("sys.modules", {"anthropic": anthropic_mod}):
            result = _make_anthropic_client().complete(
                messages=[{"role": "user", "content": "hi"}]
            )
        assert result == ""


class TestCompleteOpenAI:
    def _mock_openai(self, text: str) -> MagicMock:
        choice = MagicMock()
        choice.message.content = text
        response = MagicMock()
        response.choices = [choice]
        client_mock = MagicMock()
        client_mock.chat.completions.create.return_value = response
        openai_mod = MagicMock()
        openai_mod.OpenAI.return_value = client_mock
        openai_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        return openai_mod

    def test_returns_response_text(self) -> None:
        openai_mod = self._mock_openai("Response text")
        with patch.dict("sys.modules", {"openai": openai_mod}):
            result = _make_openai_client().complete(
                messages=[{"role": "user", "content": "hi"}]
            )
        assert result == "Response text"

    def test_prepends_system_as_message(self) -> None:
        openai_mod = self._mock_openai("ok")
        with patch.dict("sys.modules", {"openai": openai_mod}):
            _make_openai_client().complete(
                messages=[{"role": "user", "content": "hi"}],
                system="Be concise.",
            )
        call_kwargs = openai_mod.OpenAI.return_value.chat.completions.create.call_args.kwargs
        msgs = call_kwargs.get("messages", [])
        assert msgs[0] == {"role": "system", "content": "Be concise."}

    def test_raises_llm_auth_error_on_authentication_error(self) -> None:
        openai_mod = MagicMock()
        openai_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        openai_mod.OpenAI.return_value.chat.completions.create.side_effect = (
            openai_mod.AuthenticationError("expired")
        )
        with patch.dict("sys.modules", {"openai": openai_mod}):
            with pytest.raises(LLMAuthError):
                _make_openai_client().complete(
                    messages=[{"role": "user", "content": "hi"}]
                )

    def test_raises_llm_error_on_other_exception(self) -> None:
        openai_mod = MagicMock()
        openai_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
        openai_mod.OpenAI.return_value.chat.completions.create.side_effect = RuntimeError("err")
        with patch.dict("sys.modules", {"openai": openai_mod}):
            with pytest.raises(LLMError):
                _make_openai_client().complete(
                    messages=[{"role": "user", "content": "hi"}]
                )

    def test_uses_custom_base_url(self) -> None:
        openai_mod = self._mock_openai("ok")
        with patch.dict("sys.modules", {"openai": openai_mod}):
            client = LLMClient(
                api="openai-chat",
                model="gemini-2.0-flash",
                token_provider=_StaticKeyProvider("key"),
                base_url="https://vertex.example.com/v1",
            )
            client.complete(messages=[{"role": "user", "content": "hi"}])
        openai_mod.OpenAI.assert_called_once_with(
            api_key="key", base_url="https://vertex.example.com/v1"
        )


# ── 7. build_llm_client() factory ────────────────────────────────────────────


class TestBuildLLMClientFactory:
    def test_defaults_to_anthropic_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        client = build_llm_client()
        assert client.api == "anthropic-messages"

    def test_reads_api_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORY_LLM_API", "openai-chat")
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        client = build_llm_client()
        assert client.api == "openai-chat"

    def test_reads_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORY_LLM_MODEL", "my-custom-model")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        client = build_llm_client()
        assert client.model == "my-custom-model"

    def test_reads_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORY_LLM_BASE_URL", "https://custom.url")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        client = build_llm_client()
        assert client.base_url == "https://custom.url"

    def test_auto_detects_url_auth_when_memory_token_url_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_TOKEN_URL", "http://token-server/key")
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        client = build_llm_client()
        assert isinstance(client._token_provider, _URLTokenProvider)

    def test_explicit_static_key_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORY_LLM_AUTH", "static-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-123")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        client = build_llm_client()
        assert isinstance(client._token_provider, _StaticKeyProvider)
        assert client._token_provider.get_token() == "sk-123"

    def test_explicit_url_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMORY_LLM_AUTH", "url")
        monkeypatch.setenv("MEMORY_TOKEN_URL", "http://tok/key")
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        client = build_llm_client()
        assert isinstance(client._token_provider, _URLTokenProvider)

    def test_explicit_override_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        client = build_llm_client(
            api="openai-chat",
            model="gpt-4o",
            base_url="https://override.com",
        )
        assert client.api == "openai-chat"
        assert client.model == "gpt-4o"
        assert client.base_url == "https://override.com"


# ── 8. build_graphiti_llm_client() ───────────────────────────────────────────


class TestBuildGraphitiLLMClient:
    def test_anthropic_messages_returns_anthropic_client(self) -> None:
        graphiti_mod = MagicMock()
        graphiti_mod.llm_client.anthropic_client.AnthropicClient = MagicMock(return_value="ac")
        graphiti_mod.llm_client.config.LLMConfig = MagicMock(return_value="cfg")

        with patch.dict(
            "sys.modules",
            {
                "graphiti_core": MagicMock(),
                "graphiti_core.llm_client": graphiti_mod.llm_client,
                "graphiti_core.llm_client.anthropic_client": graphiti_mod.llm_client.anthropic_client,
                "graphiti_core.llm_client.config": graphiti_mod.llm_client.config,
            },
        ):
            client = LLMClient(
                api="anthropic-messages",
                model="claude-haiku",
                token_provider=_StaticKeyProvider("key"),
            )
            result = client.build_graphiti_llm_client()
        assert result == "ac"

    def test_openai_chat_returns_openai_client(self) -> None:
        graphiti_mod = MagicMock()
        graphiti_mod.llm_client.openai_client.OpenAIClient = MagicMock(return_value="oc")
        graphiti_mod.llm_client.config.LLMConfig = MagicMock(return_value="cfg")

        with patch.dict(
            "sys.modules",
            {
                "graphiti_core": MagicMock(),
                "graphiti_core.llm_client": graphiti_mod.llm_client,
                "graphiti_core.llm_client.openai_client": graphiti_mod.llm_client.openai_client,
                "graphiti_core.llm_client.config": graphiti_mod.llm_client.config,
            },
        ):
            client = LLMClient(
                api="openai-chat",
                model="gemini-2.0-flash",
                token_provider=_StaticKeyProvider("key"),
            )
            result = client.build_graphiti_llm_client()
        assert result == "oc"


# ── 9 & 10. TokenManager ─────────────────────────────────────────────────────


class TestTokenManager:
    def _make_manager(self, api: str = "anthropic-messages") -> tuple[TokenManager, LLMClient]:
        llm = LLMClient(
            api=api,
            model="claude-haiku",
            token_provider=_StaticKeyProvider("key"),
        )
        manager = TokenManager(llm_client=llm)
        return manager, llm

    def test_get_client_returns_llm_client_on_success(self) -> None:
        manager, llm = self._make_manager()
        with patch.object(llm, "probe"):
            result = manager.get_client()
        assert result is llm

    def test_get_client_raises_llm_unavailable_on_auth_error(self) -> None:
        manager, llm = self._make_manager()
        with patch.object(llm, "probe", side_effect=LLMAuthError("expired")):
            with pytest.raises(LLMUnavailable):
                manager.get_client()

    def test_get_client_raises_llm_unavailable_on_llm_error(self) -> None:
        manager, llm = self._make_manager()
        with patch.object(llm, "probe", side_effect=LLMError("timeout")):
            with pytest.raises(LLMUnavailable):
                manager.get_client()

    def test_get_client_resets_probe_failures_on_success(self) -> None:
        manager, llm = self._make_manager()
        manager._probe_failures = 3
        with patch.object(llm, "probe"):
            manager.get_client()
        assert manager._probe_failures == 0

    def test_backoff_delay_follows_schedule(self) -> None:
        from services.memory.token_manager import BACKOFF_SCHEDULE

        manager, _ = self._make_manager()
        delays = [manager.backoff_delay() for _ in range(5)]
        assert delays[0] == BACKOFF_SCHEDULE[0]
        assert delays[1] == BACKOFF_SCHEDULE[1]
        assert delays[2] == BACKOFF_SCHEDULE[2]
        # Saturates at last value.
        assert delays[3] == BACKOFF_SCHEDULE[-1]
        assert delays[4] == BACKOFF_SCHEDULE[-1]

    def test_reset_failures_zeroes_counter(self) -> None:
        manager, _ = self._make_manager()
        manager._probe_failures = 5
        manager.reset_failures()
        assert manager._probe_failures == 0

    def test_poll_until_available_returns_on_first_success(self) -> None:
        manager, llm = self._make_manager()
        with patch.object(llm, "probe"):
            result = manager.poll_until_available()
        assert result is llm

    def test_poll_until_available_retries_then_succeeds(self) -> None:
        manager, llm = self._make_manager()
        call_count = {"n": 0}

        def _probe_sometimes_fails() -> None:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise LLMAuthError("not yet")

        with (
            patch.object(llm, "probe", side_effect=_probe_sometimes_fails),
            patch("time.sleep"),
        ):
            result = manager.poll_until_available()
        assert result is llm
        assert call_count["n"] == 3
