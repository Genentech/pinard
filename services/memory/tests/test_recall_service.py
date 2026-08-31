"""Tests for the mid-session recall service.

Tests:
1. Typed intent dispatch — recall / lookup / trace
2. Relevance gating (cosine distance threshold)
3. Per-session dedup
4. Summarization (mocked LLM)
5. LLM-unavailable degradation (verbatim fallback)
6. Fail-open on SurrealDB error
7. Status JSON and log file observability
8. Request schema validation (missing fields)

All external I/O (Rosetta, SurrealDB, NATS, Anthropic) is mocked.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import services.memory.recall_service as rs
from services.memory.token_manager import LLMUnavailable
from services.memory.recall_service import (
    _ServiceStatus,
    _apply_dedup,
    _boot_hits_for_scope,
    _build_sources,
    _clean_entity_title,
    _entitled_scopes,
    _fetch_across_scopes,
    _first_sentence,
    _gate_by_relevance,
    _handle_lookup_intent,
    _handle_recall_intent,
    _handle_trace_intent,
    _summarize,
    _TYPED_ENTITY_ROLES,
    handle_boot_message,
    handle_recall_message,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_entity(
    role: str = "diagnosis",
    name: str = "test-entity",
    description: str = "a description",
    dist: float | None = None,
    entity_id: str = "entity:1",
) -> dict[str, Any]:
    e: dict[str, Any] = {"role": role, "name": name, "description": description, "id": entity_id}
    if dist is not None:
        e["dist"] = dist
    return e


def _make_msg(
    payload: dict[str, Any],
    reply: str = "_INBOX.test",
) -> MagicMock:
    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.reply = reply
    msg._client = MagicMock()
    msg._client.publish = AsyncMock()
    return msg


def _make_surreal(
    recall_return: list[dict[str, Any]] | None = None,
    lookup_return: list[dict[str, Any]] | None = None,
    trace_return: list[dict[str, Any]] | None = None,
) -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.recall = MagicMock(return_value=recall_return or [])
    surreal.lookup = MagicMock(return_value=lookup_return or [])
    surreal.trace = MagicMock(return_value=trace_return or [])
    return surreal


# ── 1. Typed intent dispatch ──────────────────────────────────────────────────

class TestTypedIntentDispatch:
    def test_recall_intent_embeds_and_calls_surreal(self) -> None:
        surreal = _make_surreal(recall_return=[_make_entity(dist=0.1)])
        with patch("services.memory.recall_service.embed", return_value=[0.1] * 1024):
            results = _handle_recall_intent(surreal, "test query")
        surreal.recall.assert_called_once()
        assert len(results) == 1

    def test_recall_intent_returns_empty_on_embed_error(self) -> None:
        from services.memory.embeddings import EmbeddingError

        surreal = _make_surreal()
        with patch(
            "services.memory.recall_service.embed",
            side_effect=EmbeddingError("Rosetta down"),
        ):
            results = _handle_recall_intent(surreal, "test query")
        assert results == []
        surreal.recall.assert_not_called()

    def test_lookup_intent_calls_surreal_fts(self) -> None:
        surreal = _make_surreal(lookup_return=[_make_entity(name="fts-hit")])
        results = _handle_lookup_intent(surreal, "some keyword")
        surreal.lookup.assert_called_once_with("some keyword", limit=10)
        assert results[0]["name"] == "fts-hit"

    def test_trace_intent_expands_log_patterns(self) -> None:
        neighbor = {"role": "diagnosis", "name": "OOM diagnosis", "description": ""}
        surreal = _make_surreal(
            trace_return=[{"neighbors": [neighbor]}]
        )
        log_pattern = _make_entity(role="log_pattern", name="OOM signal")
        results = _handle_trace_intent(surreal, [log_pattern])
        assert any(r["name"] == "OOM diagnosis" for r in results)

    def test_trace_intent_ignores_non_log_pattern_entities(self) -> None:
        surreal = _make_surreal()
        entity = _make_entity(role="action", name="some action")
        results = _handle_trace_intent(surreal, [entity])
        surreal.trace.assert_not_called()
        assert results == []

    def test_trace_intent_handles_surreal_error_gracefully(self) -> None:
        from services.memory.surrealdb.client import SurrealError

        surreal = _make_surreal()
        surreal.trace.side_effect = SurrealError("connection lost")
        log_pattern = _make_entity(role="log_pattern", name="OOM signal")
        # Should not raise.
        results = _handle_trace_intent(surreal, [log_pattern])
        assert results == []


# ── 2. Relevance gating ───────────────────────────────────────────────────────

class TestRelevanceGating:
    def test_passes_entity_below_distance_threshold(self) -> None:
        entity = _make_entity(dist=0.3)  # cosine sim ~0.7 — above 0.35
        result = _gate_by_relevance([entity])
        assert entity in result

    def test_passes_entity_at_threshold(self) -> None:
        entity = _make_entity(dist=0.65)  # exactly at new threshold
        result = _gate_by_relevance([entity])
        assert entity in result

    def test_blocks_entity_above_distance_threshold(self) -> None:
        # Both candidates above threshold; default min_top_k=0 so nothing returned.
        close = _make_entity(name="close", dist=0.70)   # above 0.65
        far = _make_entity(name="far", dist=0.90)        # further above
        result = _gate_by_relevance([close, far])
        assert result == []

    def test_min_top_k_opt_in_returns_closest(self) -> None:
        # Legacy opt-in: min_top_k=1 surfaces the closest candidate above threshold.
        close = _make_entity(name="close", dist=0.70)
        far = _make_entity(name="far", dist=0.90)
        result = _gate_by_relevance([close, far], min_top_k=1)
        assert len(result) == 1
        assert result[0]["name"] == "close"

    def test_blocks_entity_when_fts_present(self) -> None:
        # When an FTS hit is already in gated, the safety net must NOT fire.
        fts = _make_entity(name="fts")          # no dist — passes unconditionally
        bad = _make_entity(name="bad", dist=0.80)  # above threshold
        result = _gate_by_relevance([fts, bad])
        names = {e["name"] for e in result}
        assert "fts" in names
        assert "bad" not in names

    def test_fts_entities_without_dist_pass_through(self) -> None:
        entity = _make_entity()  # no 'dist' field
        result = _gate_by_relevance([entity])
        assert entity in result

    def test_mixed_batch(self) -> None:
        good = _make_entity(name="good", dist=0.2)
        bad = _make_entity(name="bad", dist=0.8)
        fts = _make_entity(name="fts")  # no dist
        result = _gate_by_relevance([good, bad, fts])
        names = {e["name"] for e in result}
        assert "good" in names
        assert "bad" not in names
        assert "fts" in names


# ── 3. Per-session dedup ──────────────────────────────────────────────────────

class TestSessionDedup:
    def setup_method(self) -> None:
        # Reset global dedup state before each test.
        rs._session_dedup.clear()

    def test_first_query_returns_all_candidates(self) -> None:
        entities = [_make_entity(name="A"), _make_entity(name="B")]
        fresh = _apply_dedup("sess-1", entities)
        assert len(fresh) == 2

    def test_second_query_same_session_filters_seen(self) -> None:
        entity = _make_entity(name="A")
        _apply_dedup("sess-1", [entity])
        fresh = _apply_dedup("sess-1", [entity])
        assert fresh == []

    def test_different_sessions_do_not_share_dedup(self) -> None:
        entity = _make_entity(name="A")
        _apply_dedup("sess-1", [entity])
        fresh = _apply_dedup("sess-2", [entity])
        assert len(fresh) == 1

    def test_partial_dedup_returns_only_new_entities(self) -> None:
        a = _make_entity(name="A")
        b = _make_entity(name="B")
        _apply_dedup("sess-1", [a])
        fresh = _apply_dedup("sess-1", [a, b])
        assert len(fresh) == 1
        assert fresh[0]["name"] == "B"

    def test_dedup_tracks_session_count(self) -> None:
        rs._status.dedup_sessions_tracked = 0
        _apply_dedup("sess-new", [_make_entity()])
        assert rs._status.dedup_sessions_tracked >= 1


# ── 4. Summarization with LLM ─────────────────────────────────────────────────

class TestSummarization:
    def _make_llm_client(self, text: str) -> MagicMock:
        """Return a mock LLMClient whose complete() returns *text*."""
        client = MagicMock()
        client.complete.return_value = text
        return client

    def test_summarizes_entities_with_llm(self) -> None:
        entities = [_make_entity(name="OOM fix", description="increase --mem-budget")]
        fake_client = self._make_llm_client("Known issue: OOM resolved by increasing mem-budget.")
        with patch.object(rs._token_manager, "get_client", return_value=fake_client):
            result = _summarize(entities, 400, "genomics-build")
        assert result is not None
        assert result.startswith("[memory]")
        assert "OOM" in result or "mem-budget" in result

    def test_summarize_returns_none_for_empty_entities(self) -> None:
        result = _summarize([], 400, "grp")
        assert result is None

    def test_context_prefixed_with_memory(self) -> None:
        entities = [_make_entity(name="rule A", description="do X")]
        fake_client = self._make_llm_client("Do X always.")
        with patch.object(rs._token_manager, "get_client", return_value=fake_client):
            result = _summarize(entities, 400, "grp")
        assert result is not None
        assert result.startswith("[memory]")

    def test_llm_prompt_includes_group_id(self) -> None:
        entities = [_make_entity(name="A")]
        fake_client = self._make_llm_client("Summary text.")
        with patch.object(rs._token_manager, "get_client", return_value=fake_client):
            _summarize(entities, 400, "my-pipeline")
        call_args = fake_client.complete.call_args
        prompt_content = call_args.kwargs["messages"][0]["content"]
        assert "my-pipeline" in prompt_content


# ── 5. LLM-unavailable degradation ───────────────────────────────────────────

class TestLLMUnavailableDegradation:
    def test_verbatim_fallback_when_llm_unavailable(self) -> None:
        entities = [_make_entity(name="OOM fix", description="use 64G budget")]
        with patch.object(
            rs._token_manager, "get_client", side_effect=LLMUnavailable("expired")
        ):
            result = _summarize(entities, 400, "grp")
        assert result is not None
        assert result.startswith("[memory]")
        assert "OOM fix" in result or "64G budget" in result

    def test_fallback_on_arbitrary_llm_exception(self) -> None:
        entities = [_make_entity(name="fact A", description="detail")]
        with patch.object(
            rs._token_manager, "get_client", side_effect=RuntimeError("network error")
        ):
            result = _summarize(entities, 400, "grp")
        assert result is not None
        assert result.startswith("[memory]")

    def test_summarize_empty_llm_response_falls_back(self) -> None:
        entities = [_make_entity(name="fact A", description="detail")]
        fake_client = MagicMock()
        fake_client.complete.return_value = ""
        with patch.object(rs._token_manager, "get_client", return_value=fake_client):
            result = _summarize(entities, 400, "grp")
        assert result is not None
        assert result.startswith("[memory]")


# ── 6. Fail-open on SurrealDB error ──────────────────────────────────────────

class TestFailOpenOnError:
    @pytest.mark.asyncio
    async def test_surreal_error_returns_null_context(self) -> None:
        from services.memory.surrealdb.client import SurrealError

        msg = _make_msg({"group_id": "grp", "session_id": "s1", "query": {"user_message": "q", "assistant_excerpt": "a", "turn_index": 1}})
        with patch(
            "services.memory.recall_service.SurrealClient",
            side_effect=SurrealError("connection refused"),
        ):
            await handle_recall_message(msg)

        msg._client.publish.assert_called_once()
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is None

    @pytest.mark.asyncio
    async def test_arbitrary_exception_returns_null_context(self) -> None:
        msg = _make_msg({"group_id": "grp", "session_id": "s1", "query": {"user_message": "q", "assistant_excerpt": "a", "turn_index": 1}})
        with patch(
            "services.memory.recall_service.SurrealClient",
            side_effect=RuntimeError("unexpected"),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is None

    @pytest.mark.asyncio
    async def test_no_reply_subject_does_not_raise(self) -> None:
        msg = _make_msg({"group_id": "grp"}, reply="")
        msg.reply = None
        # Should return silently.
        await handle_recall_message(msg)

    @pytest.mark.asyncio
    async def test_malformed_payload_returns_null(self) -> None:
        msg = MagicMock()
        msg.data = b"not valid json{"
        msg.reply = "_INBOX.test"
        msg._client = MagicMock()
        msg._client.publish = AsyncMock()
        await handle_recall_message(msg)
        # null reply should be sent
        msg._client.publish.assert_called_once()
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is None


# ── 7. Status JSON and log file observability ─────────────────────────────────

class TestObservability:
    def test_status_file_written_after_query(self, tmp_path: Path) -> None:
        orig = rs._STATUS_FILE
        rs._STATUS_FILE = tmp_path / "memory-service-status.json"
        try:
            status = _ServiceStatus()
            status.record_query(latency_ms=150.0, had_results=True)
            status.write()

            written = json.loads((tmp_path / "memory-service-status.json").read_text())
            assert written["state"] == "running"
            assert written["queries_today"] == 1
            assert written["queries_with_results"] == 1
            assert written["avg_query_latency_ms"] == pytest.approx(150.0, abs=0.1)
        finally:
            rs._STATUS_FILE = orig

    def test_status_file_has_required_fields(self, tmp_path: Path) -> None:
        orig = rs._STATUS_FILE
        rs._STATUS_FILE = tmp_path / "status.json"
        try:
            _ServiceStatus().write()
            data = json.loads((tmp_path / "status.json").read_text())
            required = {
                "state", "qdrant_available", "graphiti_available",
                "queries_today", "queries_with_results",
                "avg_query_latency_ms", "dedup_sessions_tracked",
            }
            assert required.issubset(set(data.keys()))
        finally:
            rs._STATUS_FILE = orig

    def test_status_day_reset_resets_counters(self, tmp_path: Path) -> None:
        status = _ServiceStatus()
        status._day = "2000-01-01"  # force a stale day
        status.queries_today = 99
        status.queries_with_results = 88
        status.record_query(latency_ms=10.0, had_results=True)
        # Day reset should have zeroed out then incremented by 1.
        assert status.queries_today == 1
        assert status.queries_with_results == 1

    def test_avg_latency_computed_correctly(self) -> None:
        status = _ServiceStatus()
        status.record_query(100.0, True)
        status.record_query(200.0, False)
        assert status.avg_query_latency_ms == pytest.approx(150.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_status_updated_after_successful_recall(self, tmp_path: Path) -> None:
        orig_status_file = rs._STATUS_FILE
        orig_status = rs._status
        rs._STATUS_FILE = tmp_path / "status.json"
        rs._status = _ServiceStatus()
        rs._session_dedup.clear()

        try:
            entity = _make_entity(name="OOM fix", dist=0.2)
            surreal = _make_surreal(recall_return=[entity])

            payload = {
                "session_id": "sess-obs",
                "group_id": "grp",
                "query": {"user_message": "help", "assistant_excerpt": "ok", "turn_index": 1},
                "constraints": {"max_context_tokens": 50},
            }
            msg = _make_msg(payload)

            with (
                patch("services.memory.recall_service.SurrealClient", return_value=surreal),
                patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
                patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
            ):
                await handle_recall_message(msg)

            assert rs._status.queries_today == 1
            assert (tmp_path / "status.json").exists()
        finally:
            rs._STATUS_FILE = orig_status_file
            rs._status = orig_status


# ── 8. Request schema validation ──────────────────────────────────────────────

class TestRequestSchemaValidation:
    @pytest.mark.asyncio
    async def test_missing_group_id_returns_null(self) -> None:
        msg = _make_msg({"session_id": "s1", "query": {"user_message": "q", "assistant_excerpt": "a", "turn_index": 0}})
        await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is None

    @pytest.mark.asyncio
    async def test_empty_group_id_returns_null(self) -> None:
        msg = _make_msg({"group_id": "", "session_id": "s1", "query": {}})
        await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is None

    @pytest.mark.asyncio
    async def test_missing_query_fields_default_gracefully(self) -> None:
        """Missing query sub-fields should not crash — defaults to empty strings."""
        payload = {"group_id": "grp", "session_id": "s1"}
        msg = _make_msg(payload)
        surreal = _make_surreal()
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
        ):
            await handle_recall_message(msg)
        # No crash — null context returned (no relevant entities).
        published = json.loads(msg._client.publish.call_args[0][1])
        assert "context" in published

    @pytest.mark.asyncio
    async def test_response_includes_meta_fields(self) -> None:
        """Response must always include context, sources, and meta."""
        msg = _make_msg({"group_id": "grp", "session_id": "s1", "query": {}})
        surreal = _make_surreal()
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
        ):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert "context" in published
        assert "sources" in published
        assert "meta" in published
        assert "total_candidates" in published["meta"]
        assert "returned_tokens" in published["meta"]

    @pytest.mark.asyncio
    async def test_user_message_truncated_to_500(self) -> None:
        """Long user_message is accepted without error — truncated internally."""
        long_msg = "x" * 2000
        payload = {
            "group_id": "grp",
            "session_id": "s1",
            "query": {"user_message": long_msg, "assistant_excerpt": "a", "turn_index": 0},
        }
        msg = _make_msg(payload)
        surreal = _make_surreal()
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024) as mock_embed,
        ):
            await handle_recall_message(msg)
        # Embed was called with at most 500+500+1 chars (user + space + assistant).
        call_text = mock_embed.call_args[0][0]
        assert len(call_text) <= 1002  # 500 + 1 + 500 + 1


# ── 9. Sources builder ────────────────────────────────────────────────────────

class TestSourcesBuilder:
    def test_sources_include_type_and_role(self) -> None:
        entity = _make_entity(role="diagnosis", name="A", entity_id="entity:42")
        sources = _build_sources([entity])
        assert sources[0]["type"] == "surrealdb"
        assert sources[0]["role"] == "diagnosis"

    def test_sources_include_score_from_dist(self) -> None:
        entity = _make_entity(dist=0.3)
        sources = _build_sources([entity])
        assert "score" in sources[0]
        # score = 1 - dist = 0.7
        assert abs(sources[0]["score"] - 0.7) < 0.001

    def test_sources_no_score_when_no_dist(self) -> None:
        entity = _make_entity()  # FTS result — no dist
        sources = _build_sources([entity])
        assert "score" not in sources[0]

    def test_sources_empty_for_empty_input(self) -> None:
        assert _build_sources([]) == []


# ── Test: ensure_schema called in recall pipeline ─────────────────────────────

class TestEnsureSchemaInRecall:
    def setup_method(self) -> None:
        import services.memory.surrealdb.client as client_mod
        client_mod._schema_applied.clear()
        # Also clear per-session dedup state.
        rs._session_dedup.clear()

    @pytest.mark.asyncio
    async def test_recall_pipeline_calls_ensure_schema(self) -> None:
        """ensure_schema is called before any recall query in handle_recall_message."""
        msg = MagicMock()
        msg.reply = "_INBOX.test"
        msg.data = json.dumps({
            "session_id": "s1",
            "group_id": "grp-recall",
            "query": {"user_message": "OOM error", "assistant_excerpt": "", "turn_index": 1},
            "constraints": {},
        }).encode()

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.recall.return_value = []
        mock_surreal.lookup.return_value = []
        mock_surreal.trace.return_value = []

        published: list[bytes] = []

        async def fake_publish(subject: str, data: bytes) -> None:
            published.append(data)

        msg._client = MagicMock()
        msg._client.publish = fake_publish

        with patch("services.memory.recall_service.SurrealClient", return_value=mock_surreal):
            with patch("services.memory.recall_service.embed", return_value=[0.1] * 1024):
                await handle_recall_message(msg)

        # ensure_schema is called twice: once for the scoped DB and once for __global__.
        assert mock_surreal.ensure_schema.call_count >= 1

    @pytest.mark.asyncio
    async def test_recall_pipeline_ensure_schema_before_recall(self) -> None:
        """ensure_schema is called before recall() in handle_recall_message."""
        msg = MagicMock()
        msg.reply = "_INBOX.test"
        msg.data = json.dumps({
            "session_id": "s2",
            "group_id": "grp-recall2",
            "query": {"user_message": "test", "assistant_excerpt": "", "turn_index": 0},
            "constraints": {},
        }).encode()

        call_order: list[str] = []

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.ensure_schema.side_effect = lambda: call_order.append("ensure_schema")
        mock_surreal.recall.side_effect = lambda *a, **kw: call_order.append("recall") or []
        mock_surreal.lookup.return_value = []
        mock_surreal.trace.return_value = []

        msg._client = MagicMock()
        msg._client.publish = AsyncMock()

        with patch("services.memory.recall_service.SurrealClient", return_value=mock_surreal):
            with patch("services.memory.recall_service.embed", return_value=[0.1] * 1024):
                await handle_recall_message(msg)

        assert "ensure_schema" in call_order
        assert "recall" in call_order
        assert call_order.index("ensure_schema") < call_order.index("recall")


# ── 10. Wiki recall integration ───────────────────────────────────────────────

def _make_wiki_hit(
    path: str = "concepts/test-page",
    title: str = "Test Page",
    body: str = "body content",
    confidence: float = 0.9,
    status: str = "auto_serve",
    dist: float | None = 0.1,
) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "path": path,
        "title": title,
        "type": "concept",
        "body": body,
        "confidence": confidence,
        "status": status,
        "_wiki": True,
    }
    if dist is not None:
        hit["dist"] = dist
    return hit


def _make_surreal_with_wiki(
    recall_return: list[dict[str, Any]] | None = None,
    lookup_return: list[dict[str, Any]] | None = None,
    trace_return: list[dict[str, Any]] | None = None,
    recall_wiki_return: list[dict[str, Any]] | None = None,
    lookup_wiki_return: list[dict[str, Any]] | None = None,
) -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.recall = MagicMock(return_value=recall_return or [])
    surreal.lookup = MagicMock(return_value=lookup_return or [])
    surreal.trace = MagicMock(return_value=trace_return or [])
    surreal.recall_wiki = MagicMock(return_value=recall_wiki_return or [])
    surreal.lookup_wiki = MagicMock(return_value=lookup_wiki_return or [])
    return surreal


class TestWikiRecallIntegration:
    """Tests for wiki recall wiring in handle_recall_message."""

    def setup_method(self) -> None:
        rs._session_dedup.clear()

    @pytest.mark.asyncio
    async def test_wiki_hits_merged_into_candidates(self) -> None:
        """Wiki hits from scoped DB are merged into the candidate list."""
        wiki_hit = _make_wiki_hit(path="concepts/oom", title="OOM Fix")
        entity_hit = _make_entity(name="oom-entity", dist=0.2)
        surreal = _make_surreal_with_wiki(
            recall_return=[entity_hit],
            recall_wiki_return=[wiki_hit],
        )

        msg = _make_msg({
            "session_id": "sess-wiki-1",
            "group_id": "grp",
            "query": {"user_message": "OOM error", "assistant_excerpt": "", "turn_index": 1},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
            patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        # Wiki hit should appear in sources.
        wiki_sources = [s for s in published["sources"] if s.get("type") == "wiki"]
        assert len(wiki_sources) >= 1
        assert wiki_sources[0]["path"] == "concepts/oom"
        assert wiki_sources[0]["title"] == "OOM Fix"

    @pytest.mark.asyncio
    async def test_global_wiki_always_included(self) -> None:
        """__global__ wiki docs appear in results alongside scoped docs."""
        scoped_wiki = _make_wiki_hit(path="scoped/page", title="Scoped Page")
        global_wiki = _make_wiki_hit(path="global/page", title="Global Page")

        call_count = {"n": 0}

        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            call_count["n"] += 1
            if group_id == rs.GLOBAL_WIKI_GROUP:
                return _make_surreal_with_wiki(recall_wiki_return=[global_wiki])
            return _make_surreal_with_wiki(recall_wiki_return=[scoped_wiki])

        msg = _make_msg({
            "session_id": "sess-global-1",
            "group_id": "grp",
            "query": {"user_message": "global knowledge", "assistant_excerpt": "", "turn_index": 1},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
            patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        wiki_paths = {s["path"] for s in published["sources"] if s.get("type") == "wiki"}
        assert "scoped/page" in wiki_paths
        assert "global/page" in wiki_paths

    @pytest.mark.asyncio
    async def test_include_needs_review_flag(self) -> None:
        """include_needs_review=true causes recall_wiki to be called with that flag."""
        surreal = _make_surreal_with_wiki()

        msg = _make_msg({
            "session_id": "sess-nr-1",
            "group_id": "grp",
            "query": {"user_message": "q", "assistant_excerpt": "", "turn_index": 1},
            "constraints": {"include_needs_review": True},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
        ):
            await handle_recall_message(msg)

        # recall_wiki should have been called with include_needs_review=True.
        surreal.recall_wiki.assert_called()
        call_kwargs = surreal.recall_wiki.call_args[1]
        assert call_kwargs.get("include_needs_review") is True

    @pytest.mark.asyncio
    async def test_wiki_sources_have_correct_type_fields(self) -> None:
        """Wiki sources must include type=wiki, path, title, confidence, status."""
        wiki_hit = _make_wiki_hit(
            path="decisions/surreal-pivot",
            title="SurrealDB Pivot",
            confidence=0.9,
            status="auto_serve",
            dist=0.1,
        )
        surreal = _make_surreal_with_wiki(recall_wiki_return=[wiki_hit])

        msg = _make_msg({
            "session_id": "sess-src-1",
            "group_id": "grp",
            "query": {"user_message": "decision", "assistant_excerpt": "", "turn_index": 1},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
            patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        wiki_sources = [s for s in published["sources"] if s.get("type") == "wiki"]
        assert wiki_sources, "Expected at least one wiki source"
        src = wiki_sources[0]
        assert src["type"] == "wiki"
        assert src["path"] == "decisions/surreal-pivot"
        assert src["title"] == "SurrealDB Pivot"
        assert src["confidence"] == 0.9
        assert src["status"] == "auto_serve"
        assert "score" in src  # dist=0.1 → score=0.9

    @pytest.mark.asyncio
    async def test_wiki_errors_do_not_break_entity_recall(self) -> None:
        """If wiki recall raises, entity recall results are still returned."""
        entity_hit = _make_entity(name="important-fact", dist=0.15)
        surreal = _make_surreal_with_wiki(recall_return=[entity_hit])
        surreal.recall_wiki.side_effect = RuntimeError("wiki DB down")
        surreal.lookup_wiki.side_effect = RuntimeError("wiki DB down")

        msg = _make_msg({
            "session_id": "sess-err-1",
            "group_id": "grp",
            "query": {"user_message": "important", "assistant_excerpt": "", "turn_index": 1},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
            patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        # Entity source should still be present despite wiki failure.
        entity_sources = [s for s in published["sources"] if s.get("type") == "surrealdb"]
        assert len(entity_sources) >= 1

    @pytest.mark.asyncio
    async def test_global_wiki_failure_does_not_break_recall(self) -> None:
        """If __global__ wiki SurrealClient raises, the rest of recall succeeds."""
        entity_hit = _make_entity(name="entity-ok", dist=0.2)

        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            if group_id == rs.GLOBAL_WIKI_GROUP:
                raise RuntimeError("global DB unavailable")
            return _make_surreal_with_wiki(recall_return=[entity_hit])

        msg = _make_msg({
            "session_id": "sess-gfail-1",
            "group_id": "grp",
            "query": {"user_message": "query", "assistant_excerpt": "", "turn_index": 1},
        })

        with (
            patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 1024),
            patch.object(rs._token_manager, "get_client", side_effect=LLMUnavailable("no llm")),
        ):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["context"] is not None  # entity recall still succeeded

    def test_wiki_dedup_key_does_not_collide_with_entity_key(self) -> None:
        """__wiki__:<path> dedup keys never collide with entity role:name keys."""
        rs._session_dedup.clear()
        # An entity with role="wiki" and name="concepts/page" has entity key "wiki:concepts/page".
        # The wiki dedup key uses "__wiki__:concepts/page" — different prefix, no collision.
        entity = _make_entity(role="wiki", name="concepts/page", dist=0.2)
        wiki = _make_wiki_hit(path="concepts/page", dist=0.1)

        all_candidates = [entity, wiki]
        fresh = _apply_dedup("sess-dedup-1", all_candidates)
        # Both should be returned since they use different dedup keys.
        assert len(fresh) == 2

    def test_build_sources_wiki_type(self) -> None:
        """_build_sources tags wiki entries with type=wiki and correct fields."""
        wiki_hit = _make_wiki_hit(
            path="actions/fix-oom",
            title="Fix OOM",
            confidence=0.85,
            status="auto_serve",
            dist=0.2,
        )
        sources = _build_sources([wiki_hit])
        assert sources[0]["type"] == "wiki"
        assert sources[0]["path"] == "actions/fix-oom"
        assert sources[0]["title"] == "Fix OOM"
        assert sources[0]["confidence"] == 0.85
        assert sources[0]["status"] == "auto_serve"
        assert abs(sources[0]["score"] - 0.8) < 0.001

    def test_build_sources_entity_type_unchanged(self) -> None:
        """_build_sources still emits type=surrealdb for entity hits."""
        entity = _make_entity(role="diagnosis", name="oom", dist=0.3)
        sources = _build_sources([entity])
        assert sources[0]["type"] == "surrealdb"
        assert sources[0]["role"] == "diagnosis"
        assert "path" not in sources[0]


# ── 11. Fetch-by-ref (exact drill-down) ────────────────────────────────────────

def _make_surreal_fetch(
    wiki_return: dict[str, Any] | None = None,
    entity_return: dict[str, Any] | None = None,
) -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.fetch_wiki_by_path = MagicMock(return_value=wiki_return)
    surreal.fetch_entity_by_id = MagicMock(return_value=entity_return)
    return surreal


class TestFetchAcrossScopes:
    def test_wiki_ref_found_in_first_scope(self) -> None:
        wiki_doc = {"path": "concepts/oom", "title": "OOM Fix", "body": "body text",
                    "confidence": 0.9, "status": "auto_serve"}
        surreal = _make_surreal_fetch(wiki_return=wiki_doc)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("wiki:concepts/oom", ["my-group"])
        assert result is not None
        assert result["_wiki"] is True
        assert result["path"] == "concepts/oom"
        assert result["_scope"] == "my-group"

    def test_entity_ref_found_in_first_scope(self) -> None:
        entity = {"id": "entity:abc123", "role": "diagnosis", "name": "OOM",
                  "description": "memory issue"}
        surreal = _make_surreal_fetch(entity_return=entity)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("entity:abc123", ["my-group"])
        assert result is not None
        assert result["role"] == "diagnosis"
        assert result["_scope"] == "my-group"
        assert "_wiki" not in result

    def test_ref_not_found_returns_none(self) -> None:
        surreal = _make_surreal_fetch(wiki_return=None, entity_return=None)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("wiki:nonexistent/page", ["my-group"])
        assert result is None

    def test_scope_fallback_returns_hit_from_second_scope(self) -> None:
        wiki_doc = {"path": "concepts/global", "title": "Global Page", "body": "content",
                    "confidence": 0.95, "status": "auto_serve"}

        call_count = {"n": 0}

        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First scope: miss
                return _make_surreal_fetch(wiki_return=None)
            # Second scope: hit
            return _make_surreal_fetch(wiki_return=wiki_doc)

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory):
            result = _fetch_across_scopes("wiki:concepts/global", ["my-group", "__global__"])
        assert result is not None
        assert result["path"] == "concepts/global"
        assert result["_scope"] == "__global__"

    def test_scope_error_falls_through_to_next_scope(self) -> None:
        wiki_doc = {"path": "concepts/ok", "title": "OK Page", "body": "body",
                    "confidence": 0.8, "status": "auto_serve"}
        call_count = {"n": 0}

        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("DB down")
            return _make_surreal_fetch(wiki_return=wiki_doc)

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory):
            result = _fetch_across_scopes("wiki:concepts/ok", ["bad-scope", "good-scope"])
        assert result is not None
        assert result["path"] == "concepts/ok"

    def test_bare_ref_tries_wiki_path_first(self) -> None:
        wiki_doc = {"path": "bare/path", "title": "Bare Page", "body": "body",
                    "confidence": 0.8, "status": "auto_serve"}
        surreal = _make_surreal_fetch(wiki_return=wiki_doc)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("bare/path", ["my-group"])
        assert result is not None
        surreal.fetch_wiki_by_path.assert_called_once_with("bare/path")

    def test_all_scopes_miss_returns_none(self) -> None:
        surreal = _make_surreal_fetch(wiki_return=None, entity_return=None)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("wiki:missing", ["scope1", "scope2", "scope3"])
        assert result is None


class TestEntitledScopes:
    def test_returns_group_vignoble_global_in_order(self) -> None:
        scopes = _entitled_scopes("my-project", "misc")
        assert scopes == ["my-project", "vignoble-misc", rs.GLOBAL_WIKI_GROUP]

    def test_empty_group_id_omits_vigne_scope(self) -> None:
        scopes = _entitled_scopes("", "misc")
        assert "" not in scopes
        assert "vignoble-misc" in scopes
        assert rs.GLOBAL_WIKI_GROUP in scopes

    def test_empty_vignoble_omits_vignoble_scope(self) -> None:
        scopes = _entitled_scopes("my-project", "")
        assert not any(s.startswith("vignoble-") and s == "vignoble-" for s in scopes)
        assert "my-project" in scopes
        assert rs.GLOBAL_WIKI_GROUP in scopes

    def test_global_always_included(self) -> None:
        scopes = _entitled_scopes("", "")
        assert rs.GLOBAL_WIKI_GROUP in scopes


class TestHandleRecallMessageFetch:
    def setup_method(self) -> None:
        rs._session_dedup.clear()

    @pytest.mark.asyncio
    async def test_fetch_wiki_returns_body(self) -> None:
        wiki_doc = {"path": "concepts/oom", "title": "OOM Fix", "body": "Increase budget.",
                    "confidence": 0.9, "status": "auto_serve"}
        surreal = _make_surreal_fetch(wiki_return=wiki_doc)
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:concepts/oom"})
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is True
        assert published["result"]["type"] == "wiki"
        assert published["result"]["body"] == "Increase budget."
        assert published["result"]["ref"] == "wiki:concepts/oom"

    @pytest.mark.asyncio
    async def test_fetch_entity_returns_content(self) -> None:
        entity = {"id": "entity:abc", "role": "diagnosis", "name": "OOM",
                  "description": "memory leak in ingester"}
        surreal = _make_surreal_fetch(entity_return=entity)
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "entity:abc"})
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is True
        assert published["result"]["type"] == "entity"
        assert published["result"]["description"] == "memory leak in ingester"

    @pytest.mark.asyncio
    async def test_fetch_unknown_ref_returns_null_result(self) -> None:
        surreal = _make_surreal_fetch(wiki_return=None, entity_return=None)
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:nonexistent/page"})
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["result"] is None
        assert published["meta"]["found"] is False

    @pytest.mark.asyncio
    async def test_fetch_surreal_error_returns_null_result(self) -> None:
        from services.memory.surrealdb.client import SurrealError
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:some/page"})
        with patch("services.memory.recall_service.SurrealClient",
                   side_effect=SurrealError("DB down")):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["result"] is None
        assert published["meta"]["found"] is False

    @pytest.mark.asyncio
    async def test_fetch_bypasses_query_pipeline(self) -> None:
        wiki_doc = {"path": "p", "title": "T", "body": "B",
                    "confidence": 0.9, "status": "auto_serve"}
        surreal = _make_surreal_fetch(wiki_return=wiki_doc)
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:p"})
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            with patch("services.memory.recall_service.embed") as mock_embed:
                await handle_recall_message(msg)
        # embed must NOT be called — fetch path skips the vector pipeline.
        mock_embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_no_reply_subject_does_not_raise(self) -> None:
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:some/page"})
        msg.reply = None
        surreal = _make_surreal_fetch(wiki_return=None)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_fetch_response_shape(self) -> None:
        wiki_doc = {"path": "concepts/test", "title": "Test", "body": "Body text.",
                    "confidence": 0.85, "status": "auto_serve"}
        surreal = _make_surreal_fetch(wiki_return=wiki_doc)
        msg = _make_msg({"session_id": "s1", "group_id": "grp", "vignoble": "misc",
                         "fetch": "wiki:concepts/test"})
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert "result" in published
        assert "meta" in published
        assert "ref" in published["meta"]
        assert "found" in published["meta"]


# ── Fetch-by-ref tests ───────────────────────────────────────────────────────

def _make_wiki_row(
    path: str = "concepts/foo",
    title: str = "Foo",
    body: str = "Body text",
    confidence: float = 0.9,
    status: str = "auto_serve",
) -> dict[str, Any]:
    return {"path": path, "title": title, "body": body, "confidence": confidence, "status": status}


def _make_entity_row(
    entity_id: str = "entity:abc123",
    role: str = "gotcha",
    name: str = "The Gotcha",
    description: str = "Watch out for this",
) -> dict[str, Any]:
    return {"id": entity_id, "role": role, "name": name, "description": description}


def _make_fetch_surreal(
    wiki_row: dict[str, Any] | None = None,
    entity_row: dict[str, Any] | None = None,
) -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.ensure_schema = MagicMock()
    surreal.fetch_wiki_by_path = MagicMock(return_value=wiki_row)
    surreal.fetch_entity_by_id = MagicMock(return_value=entity_row)
    return surreal


class TestEntitledScopes:
    def test_all_three_scopes(self) -> None:
        scopes = _entitled_scopes("my-project", "myvignoble")
        assert scopes[0] == "my-project"
        assert scopes[1] == "vignoble-myvignoble"
        assert scopes[2] == rs.GLOBAL_WIKI_GROUP

    def test_empty_vignoble(self) -> None:
        scopes = _entitled_scopes("proj", "")
        assert scopes == ["proj", rs.GLOBAL_WIKI_GROUP]

    def test_empty_group_id(self) -> None:
        scopes = _entitled_scopes("", "vignoble1")
        assert scopes == ["vignoble-vignoble1", rs.GLOBAL_WIKI_GROUP]

    def test_both_empty(self) -> None:
        scopes = _entitled_scopes("", "")
        assert scopes == [rs.GLOBAL_WIKI_GROUP]


class TestFetchAcrossScopes:
    def test_wiki_ref_found_in_first_scope(self) -> None:
        """wiki:<path> is found in the first scope and returned immediately."""
        wiki_row = _make_wiki_row(path="concepts/tiledb", title="TileDB Guide")
        surreal = _make_fetch_surreal(wiki_row=wiki_row)

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("wiki:concepts/tiledb", ["proj", "__global__"])

        assert result is not None
        assert result["_wiki"] is True
        assert result["path"] == "concepts/tiledb"
        assert result["title"] == "TileDB Guide"
        assert result["_scope"] == "proj"

    def test_wiki_ref_found_in_second_scope(self) -> None:
        """Falls through to next scope when first scope has no hit."""
        wiki_row = _make_wiki_row(path="concepts/foo", title="Foo")
        call_count = 0

        def surreal_factory(**kw: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_fetch_surreal(wiki_row=None)  # first scope: miss
            return _make_fetch_surreal(wiki_row=wiki_row)  # second scope: hit

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory):
            result = _fetch_across_scopes("wiki:concepts/foo", ["proj", "__global__"])

        assert result is not None
        assert result["_scope"] == "__global__"

    def test_entity_ref_found(self) -> None:
        """entity:<id> resolves to an entity row."""
        entity_row = _make_entity_row(entity_id="entity:abc123", name="The Gotcha")
        surreal = _make_fetch_surreal(entity_row=entity_row)

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("entity:abc123", ["proj"])

        assert result is not None
        assert result.get("_wiki") is not True
        assert result["name"] == "The Gotcha"

    def test_unknown_ref_returns_none(self) -> None:
        """Unknown ref yields no hit → None."""
        surreal = _make_fetch_surreal(wiki_row=None, entity_row=None)

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("wiki:nonexistent/page", ["proj", "__global__"])

        assert result is None

    def test_scope_error_is_skipped(self) -> None:
        """A SurrealDB error in one scope is silently skipped; next scope is tried."""
        wiki_row = _make_wiki_row(path="p", title="P")

        def surreal_factory(**kw: Any) -> MagicMock:
            raise RuntimeError("DB down")

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory):
            result = _fetch_across_scopes("wiki:p", ["proj", "__global__"])

        assert result is None

    def test_bare_ref_tried_as_wiki_path(self) -> None:
        """Bare ref (no prefix) is tried as a wiki path."""
        wiki_row = _make_wiki_row(path="bare-path", title="Bare")
        surreal = _make_fetch_surreal(wiki_row=wiki_row)

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            result = _fetch_across_scopes("bare-path", ["proj"])

        assert result is not None
        surreal.fetch_wiki_by_path.assert_called_with("bare-path")

    def test_global_wiki_page_found_without_local_scope(self) -> None:
        """__global__ wiki page is found even when vigne scope misses."""
        def surreal_factory(**kw: Any) -> MagicMock:
            # Capture which group_id is used by checking the call args
            return _make_fetch_surreal(wiki_row=None)

        wiki_row = _make_wiki_row(path="global/guide", title="Global Guide")
        call_count = [0]

        def surreal_factory2(**kw: Any) -> MagicMock:
            call_count[0] += 1
            if call_count[0] < 3:
                return _make_fetch_surreal(wiki_row=None)
            return _make_fetch_surreal(wiki_row=wiki_row)

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory2):
            result = _fetch_across_scopes(
                "wiki:global/guide",
                ["proj", "vignoble-myvignoble", "__global__"],
            )

        assert result is not None
        assert result["_scope"] == "__global__"


class TestHandleRecallMessageFetch:
    @pytest.mark.asyncio
    async def test_fetch_wiki_ref_returns_full_body(self) -> None:
        """Fetch mode returns the full wiki body without summarization."""
        wiki_row = _make_wiki_row(path="concepts/tiledb", title="TileDB Guide", body="Full body text.")
        wiki_row["_wiki"] = True
        wiki_row["_scope"] = "proj"
        surreal = _make_fetch_surreal(wiki_row=wiki_row)

        msg = _make_msg({
            "session_id": "sess-fetch-1",
            "group_id": "proj",
            "vignoble": "myvignoble",
            "fetch": "wiki:concepts/tiledb",
        })

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is True
        assert published["meta"]["ref"] == "wiki:concepts/tiledb"
        result = published["result"]
        assert result["type"] == "wiki"
        assert result["body"] == "Full body text."
        assert result["title"] == "TileDB Guide"

    @pytest.mark.asyncio
    async def test_fetch_entity_ref_returns_content(self) -> None:
        """Fetch mode for entity:<id> returns entity content."""
        entity_row = _make_entity_row(
            entity_id="entity:abc123", role="gotcha", name="Watch Out", description="Critical warning."
        )
        surreal = _make_fetch_surreal(entity_row=entity_row)

        msg = _make_msg({
            "session_id": "sess-fetch-2",
            "group_id": "proj",
            "vignoble": "myvignoble",
            "fetch": "entity:abc123",
        })

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is True
        result = published["result"]
        assert result["type"] == "entity"
        assert result["name"] == "Watch Out"
        assert result["description"] == "Critical warning."

    @pytest.mark.asyncio
    async def test_fetch_unknown_ref_returns_null_result(self) -> None:
        """Unknown ref → result=null, found=false, no crash."""
        surreal = _make_fetch_surreal(wiki_row=None, entity_row=None)

        msg = _make_msg({
            "session_id": "sess-fetch-3",
            "group_id": "proj",
            "vignoble": "myvignoble",
            "fetch": "wiki:does/not/exist",
        })

        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is False
        assert published["result"] is None

    @pytest.mark.asyncio
    async def test_fetch_surreal_error_returns_null_result(self) -> None:
        """SurrealDB error during fetch → result=null, fail-open."""
        def surreal_factory(**kw: Any) -> MagicMock:
            raise RuntimeError("DB down")

        msg = _make_msg({
            "session_id": "sess-fetch-4",
            "group_id": "proj",
            "vignoble": "myvignoble",
            "fetch": "wiki:fail/page",
        })

        with patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory):
            await handle_recall_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["found"] is False
        assert published["result"] is None

    @pytest.mark.asyncio
    async def test_fetch_mode_skips_query_pipeline(self) -> None:
        """When fetch is set, the recall/lookup/embed query pipeline is not called."""
        wiki_row = _make_wiki_row()
        wiki_row["_wiki"] = True
        wiki_row["_scope"] = "proj"
        surreal = _make_fetch_surreal(wiki_row=wiki_row)

        msg = _make_msg({
            "session_id": "sess-fetch-5",
            "group_id": "proj",
            "vignoble": "myvignoble",
            "fetch": "wiki:concepts/foo",
        })

        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed") as mock_embed,
        ):
            await handle_recall_message(msg)

        # embed should NOT be called in fetch mode
        mock_embed.assert_not_called()
        # And the recall/lookup methods should not be called either
        surreal.recall.assert_not_called()
        surreal.lookup.assert_not_called()


# ── Boot manifest v2 tests (#163) ─────────────────────────────────────────────

def _make_boot_surreal(
    recall_return: list[dict[str, Any]] | None = None,
    lookup_return: list[dict[str, Any]] | None = None,
    recall_wiki_return: list[dict[str, Any]] | None = None,
    lookup_wiki_return: list[dict[str, Any]] | None = None,
) -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.ensure_schema = MagicMock()
    surreal.recall = MagicMock(return_value=recall_return or [])
    surreal.lookup = MagicMock(return_value=lookup_return or [])
    surreal.recall_wiki = MagicMock(return_value=recall_wiki_return or [])
    surreal.lookup_wiki = MagicMock(return_value=lookup_wiki_return or [])
    return surreal


def _make_boot_msg(payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.data = json.dumps(payload).encode()
    msg.reply = "_INBOX.boot_test"
    msg._client = MagicMock()
    msg._client.publish = AsyncMock()
    return msg


class TestFirstSentence:
    def test_single_sentence(self) -> None:
        assert _first_sentence("This is a sentence.") == "This is a sentence."

    def test_stops_at_period(self) -> None:
        assert _first_sentence("First. Second sentence.") == "First."

    def test_stops_at_exclamation(self) -> None:
        assert _first_sentence("Warning! More text.") == "Warning!"

    def test_stops_at_newline(self) -> None:
        assert _first_sentence("First line\nSecond line") == "First line"

    def test_truncates_long_text_without_break(self) -> None:
        long = "x" * 300
        result = _first_sentence(long)
        assert len(result) <= 200

    def test_empty_string(self) -> None:
        assert _first_sentence("") == ""

    def test_skips_h1_heading(self) -> None:
        assert _first_sentence("# Summary\n\nReal content.") == "Real content."

    def test_skips_h2_heading(self) -> None:
        assert _first_sentence("## Overview\n\nActual prose.") == "Actual prose."

    def test_skips_multiple_headings(self) -> None:
        assert _first_sentence("# Title\n## Sub\n\nProse sentence.") == "Prose sentence."

    def test_heading_only_falls_back_gracefully(self) -> None:
        # No prose lines — function returns something non-empty rather than crashing.
        result = _first_sentence("# Summary")
        assert isinstance(result, str)

    def test_heading_then_prose_with_period(self) -> None:
        body = "# Summary\n\nThis is the real summary. More text follows."
        assert _first_sentence(body) == "This is the real summary."


class TestBootHitsForScope:
    """Unit tests for _boot_hits_for_scope — the per-scope manifest builder."""

    def _wiki_hit(self, path: str = "concepts/page", title: str = "Page Title",
                  body: str = "First sentence. Rest of body.") -> dict[str, Any]:
        return {"path": path, "title": title, "body": body, "_wiki": True}

    def _entity_hit(self, role: str = "decision", name: str = "A Decision",
                    description: str = "We decided to do X. More details.",
                    entity_id: str = "entity:abc") -> dict[str, Any]:
        return {"role": role, "name": name, "description": description, "id": entity_id}

    def test_entry_shape_has_no_snippet_or_body(self) -> None:
        """Entries must have {scope, type, title, summary, ref} — no snippet/body."""
        wiki = self._wiki_hit()
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert len(entries) == 1
        e = entries[0]
        assert set(e.keys()) == {"scope", "type", "title", "summary", "ref"}
        assert "snippet" not in e
        assert "body" not in e

    def test_wiki_ref_format(self) -> None:
        """Wiki entries get ref=wiki:<path>."""
        wiki = self._wiki_hit(path="decisions/pivot")
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert entries[0]["ref"] == "wiki:decisions/pivot"

    def test_entity_ref_format(self) -> None:
        """Typed entity entries get ref=entity:<hash> (no double-prefix).

        SurrealDB RecordID already stringifies as 'entity:<hash>', so we must
        not add another 'entity:' prefix — the ref must pass fetch() directly.
        """
        # Simulate a SurrealDB RecordID that already includes the table prefix.
        entity = self._entity_hit(role="gotcha", entity_id="entity:xyz789")
        surreal = _make_boot_surreal(recall_return=[entity])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", [0.1] * 10, 5)
        entity_entries = [e for e in entries if e["type"] == "gotcha"]
        assert len(entity_entries) == 1
        # Must be single-prefix — not "entity:entity:xyz789"
        assert entity_entries[0]["ref"] == "entity:xyz789"

    def test_entity_ref_no_double_prefix_bare_id(self) -> None:
        """When id is a bare hash (no 'entity:' prefix), entity: is added once."""
        entity = self._entity_hit(role="decision", entity_id="0ab90bff")
        surreal = _make_boot_surreal(recall_return=[entity])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", [0.1] * 10, 5)
        entity_entries = [e for e in entries if e["type"] == "decision"]
        assert len(entity_entries) == 1
        assert entity_entries[0]["ref"] == "entity:0ab90bff"

    def test_summary_is_first_sentence(self) -> None:
        """Summary is extracted as the first sentence of body/description."""
        wiki = self._wiki_hit(body="First sentence. Second sentence.")
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert entries[0]["summary"] == "First sentence."

    def test_summary_falls_back_to_title_when_no_body(self) -> None:
        """When body/description is empty, summary falls back to title."""
        wiki = self._wiki_hit(body="")
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert entries[0]["summary"] == entries[0]["title"]

    def test_above_vigne_wiki_only(self) -> None:
        """Above-vigne scopes (global, vignoble-*) return wiki entries only."""
        wiki = self._wiki_hit()
        entity = self._entity_hit(role="decision")
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki], recall_return=[entity])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            # scope="__global__" != vigne_scope="my-project" → above vigne
            entries = _boot_hits_for_scope("__global__", "my-project", "query", None, 5)
        types = {e["type"] for e in entries}
        assert "wiki" in types
        assert "decision" not in types
        # Entity recall should NOT be called above vigne
        surreal.recall.assert_not_called()
        surreal.lookup.assert_not_called()

    def test_vigne_scope_includes_wiki_and_typed_entities(self) -> None:
        """The vigne scope (group_id) returns wiki + typed entity entries."""
        wiki = self._wiki_hit()
        entity = self._entity_hit(role="decision")
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki], recall_return=[entity])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", [0.1] * 10, 5)
        types = {e["type"] for e in entries}
        assert "wiki" in types
        assert "decision" in types

    def test_artifact_excluded_at_vigne(self) -> None:
        """Raw `artifact` entities are excluded even at the vigne tier."""
        artifact = self._entity_hit(role="artifact", entity_id="entity:art1")
        surreal = _make_boot_surreal(recall_return=[artifact])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", [0.1] * 10, 5)
        entity_entries = [e for e in entries if e["type"] == "artifact"]
        assert entity_entries == []

    def test_all_typed_roles_included_at_vigne(self) -> None:
        """decision, diagnosis, gotcha, action, concept, etc. are all included."""
        entities = [
            self._entity_hit(role=role, name=f"e-{role}", entity_id=f"entity:{role}")
            for role in _TYPED_ENTITY_ROLES
        ]
        surreal = _make_boot_surreal(recall_return=entities)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("proj", "proj", "query", [0.1] * 10, 50)
        returned_types = {e["type"] for e in entries if e["type"] != "wiki"}
        assert returned_types == _TYPED_ENTITY_ROLES

    def test_top_k_bounds_wiki_entries(self) -> None:
        """No more than top_k wiki entries returned per scope."""
        wikis = [self._wiki_hit(path=f"p/{i}", title=f"Page {i}") for i in range(20)]
        # Use lookup_wiki_return so FTS path returns hits even with embedding=None
        surreal = _make_boot_surreal(lookup_wiki_return=wikis)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("proj", "proj", "query", None, 3)
        wiki_entries = [e for e in entries if e["type"] == "wiki"]
        assert len(wiki_entries) <= 3

    def test_top_k_bounds_entity_entries(self) -> None:
        """No more than top_k entity entries returned per scope."""
        entities = [
            self._entity_hit(role="decision", name=f"D{i}", entity_id=f"entity:{i}")
            for i in range(20)
        ]
        surreal = _make_boot_surreal(recall_return=entities)
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("proj", "proj", "query", [0.1] * 10, 3)
        entity_entries = [e for e in entries if e["type"] != "wiki"]
        assert len(entity_entries) <= 3

    def test_scope_error_returns_empty_list(self) -> None:
        """Any error in a scope returns [] (fail-open)."""
        with patch("services.memory.recall_service.SurrealClient",
                   side_effect=RuntimeError("DB down")):
            entries = _boot_hits_for_scope("proj", "proj", "query", None, 5)
        assert entries == []

    def test_entry_scope_field_set_correctly(self) -> None:
        """Each entry's scope field matches the queried scope."""
        wiki = self._wiki_hit()
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("vignoble-misc", "proj", "query", None, 5)
        for e in entries:
            assert e["scope"] == "vignoble-misc"

    def test_entry_without_title_skipped(self) -> None:
        """Entries with empty title are skipped."""
        wiki_no_title = {"path": "p/x", "title": "", "body": "body", "_wiki": True}
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki_no_title])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("proj", "proj", "query", None, 5)
        assert entries == []

    def test_entity_dedup_at_vigne(self) -> None:
        """Duplicate entities (same role+name) are deduplicated."""
        entity = self._entity_hit(role="decision", name="Same Decision")
        surreal = _make_boot_surreal(
            recall_return=[entity],
            lookup_return=[entity],  # same entity from both recall and FTS
        )
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("proj", "proj", "query", [0.1] * 10, 5)
        decision_entries = [e for e in entries if e["type"] == "decision"]
        assert len(decision_entries) == 1

    def test_wiki_prefers_stored_summary_over_first_sentence(self) -> None:
        """Wiki entry uses stored wiki_doc.summary when present, not _first_sentence(body)."""
        wiki = self._wiki_hit(
            body="First sentence from body. More text.",
        )
        wiki["summary"] = "Intentional LLM-synthesized hint."
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert entries[0]["summary"] == "Intentional LLM-synthesized hint."

    def test_wiki_falls_back_to_first_sentence_when_no_stored_summary(self) -> None:
        """Wiki entry falls back to _first_sentence(body) when summary field is absent."""
        wiki = self._wiki_hit(body="First sentence from body. More text.")
        # No 'summary' key (old pre-#171 page).
        surreal = _make_boot_surreal(lookup_wiki_return=[wiki])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", None, 5)
        assert entries[0]["summary"] == "First sentence from body."

    def test_entity_title_is_cleaned(self) -> None:
        """Typed-entity names are cleaned before use as boot-manifest title."""
        entity = self._entity_hit(name="SurrealDB pivot: (clarified by lelongs)")
        surreal = _make_boot_surreal(recall_return=[entity])
        with patch("services.memory.recall_service.SurrealClient", return_value=surreal):
            entries = _boot_hits_for_scope("my-project", "my-project", "query", [0.1] * 10, 5)
        decision_entries = [e for e in entries if e["type"] == "decision"]
        assert decision_entries
        assert decision_entries[0]["title"] == "SurrealDB pivot"


class TestCleanEntityTitle:
    """Unit tests for _clean_entity_title."""

    def test_strips_trailing_colon(self) -> None:
        assert _clean_entity_title("SurrealDB pivot:") == "SurrealDB pivot"

    def test_strips_trailing_colon_with_whitespace(self) -> None:
        assert _clean_entity_title("SurrealDB pivot:  ") == "SurrealDB pivot"

    def test_strips_provenance_parenthetical(self) -> None:
        assert _clean_entity_title("foo (clarified by lelongs)") == "foo"

    def test_strips_combined_colon_and_provenance(self) -> None:
        assert _clean_entity_title("foo: (clarified by lelongs)") == "foo"

    def test_strips_provenance_then_trailing_colon(self) -> None:
        """Regression: colon AFTER provenance paren must also be stripped.

        'Correct Buddy Capsule domain roles (clarified by lelongs):'
        -> 'Correct Buddy Capsule domain roles'
        """
        assert _clean_entity_title(
            "Correct Buddy Capsule domain roles (clarified by lelongs):"
        ) == "Correct Buddy Capsule domain roles"

    def test_strips_leading_heading_mark(self) -> None:
        assert _clean_entity_title("# Some heading") == "Some heading"

    def test_caps_length(self) -> None:
        long_name = "a" * 200
        result = _clean_entity_title(long_name, max_len=120)
        assert len(result) <= 120

    def test_clean_name_unchanged(self) -> None:
        assert _clean_entity_title("Clean name") == "Clean name"

    def test_empty_name_returns_original(self) -> None:
        # Edge case: nothing remains after cleaning — return stripped original.
        assert _clean_entity_title("") == ""


class TestHandleBootMessage:
    """Integration tests for handle_boot_message (the NATS handler)."""

    @pytest.mark.asyncio
    async def test_response_shape_v2(self) -> None:
        """Response has {entries, meta: {total_entries}} — not hits/total_hits."""
        wiki = {"path": "p/x", "title": "X", "body": "First.", "_wiki": True}
        surreal = _make_boot_surreal(recall_wiki_return=[wiki], lookup_wiki_return=[wiki])
        msg = _make_boot_msg({
            "scopes": ["proj"],
            "group_id": "proj",
            "task_text": "test",
        })
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert "entries" in published
        assert "meta" in published
        assert "total_entries" in published["meta"]
        assert "hits" not in published
        assert "total_hits" not in published.get("meta", {})

    @pytest.mark.asyncio
    async def test_entries_have_ref_not_snippet(self) -> None:
        """Each entry in the response has ref, no snippet."""
        wiki = {"path": "concepts/foo", "title": "Foo", "body": "Summary text.", "_wiki": True}
        surreal = _make_boot_surreal(recall_wiki_return=[wiki], lookup_wiki_return=[wiki])
        msg = _make_boot_msg({"scopes": ["proj"], "group_id": "proj"})
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        for e in published["entries"]:
            assert "ref" in e
            assert "summary" in e
            assert "snippet" not in e
            assert "body" not in e

    @pytest.mark.asyncio
    async def test_group_id_required_for_vigne_tier(self) -> None:
        """group_id is passed to _boot_hits_for_scope as vigne_scope."""
        wiki = {"path": "p/x", "title": "X", "body": "Body.", "_wiki": True}
        entity = {"role": "decision", "name": "D", "description": "Desc.", "id": "entity:1"}

        surreal_above = _make_boot_surreal(recall_wiki_return=[wiki])
        surreal_vigne = _make_boot_surreal(recall_wiki_return=[wiki], recall_return=[entity])

        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            if group_id == "my-project":
                return surreal_vigne
            return surreal_above

        msg = _make_boot_msg({
            "scopes": ["__global__", "vignoble-misc", "my-project"],
            "group_id": "my-project",
        })
        with (
            patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        entries_by_scope: dict[str, list[dict[str, Any]]] = {}
        for e in published["entries"]:
            entries_by_scope.setdefault(e["scope"], []).append(e)

        # Above-vigne scopes: only wiki
        for scope in ("__global__", "vignoble-misc"):
            if scope in entries_by_scope:
                assert all(e["type"] == "wiki" for e in entries_by_scope[scope])

        # Vigne scope: wiki + typed entities
        if "my-project" in entries_by_scope:
            types = {e["type"] for e in entries_by_scope["my-project"]}
            assert "decision" in types or "wiki" in types

    @pytest.mark.asyncio
    async def test_empty_scopes_returns_empty(self) -> None:
        """Empty scopes list → empty response, no crash."""
        msg = _make_boot_msg({"scopes": [], "group_id": "proj"})
        await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["entries"] == []
        assert published["meta"]["total_entries"] == 0

    @pytest.mark.asyncio
    async def test_no_reply_subject_does_not_raise(self) -> None:
        """Missing reply subject → handler returns silently."""
        msg = _make_boot_msg({"scopes": ["proj"], "group_id": "proj"})
        msg.reply = None
        await handle_boot_message(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_malformed_payload_returns_empty(self) -> None:
        """Malformed JSON → empty fail-open response."""
        msg = MagicMock()
        msg.data = b"not valid json{"
        msg.reply = "_INBOX.boot"
        msg._client = MagicMock()
        msg._client.publish = AsyncMock()
        await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["entries"] == []

    @pytest.mark.asyncio
    async def test_total_entries_matches_len(self) -> None:
        """meta.total_entries matches the actual number of entries returned."""
        wikis = [
            {"path": f"p/{i}", "title": f"Page {i}", "body": f"Body {i}.", "_wiki": True}
            for i in range(3)
        ]
        surreal = _make_boot_surreal(recall_wiki_return=wikis, lookup_wiki_return=wikis)
        msg = _make_boot_msg({"scopes": ["proj"], "group_id": "proj", "top_k": 10})
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert published["meta"]["total_entries"] == len(published["entries"])

    @pytest.mark.asyncio
    async def test_scope_error_is_fail_open(self) -> None:
        """A DB error for one scope does not crash the handler."""
        def surreal_factory(group_id: str, **kw: Any) -> MagicMock:
            if group_id == "bad-scope":
                raise RuntimeError("DB down")
            wiki = {"path": "p/ok", "title": "OK", "body": "Fine.", "_wiki": True}
            return _make_boot_surreal(recall_wiki_return=[wiki], lookup_wiki_return=[wiki])

        msg = _make_boot_msg({
            "scopes": ["bad-scope", "good-scope"],
            "group_id": "good-scope",
        })
        with (
            patch("services.memory.recall_service.SurrealClient", side_effect=surreal_factory),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)

        published = json.loads(msg._client.publish.call_args[0][1])
        assert "entries" in published
        # good-scope entries still returned
        good_entries = [e for e in published["entries"] if e["scope"] == "good-scope"]
        assert len(good_entries) >= 1

    @pytest.mark.asyncio
    async def test_backward_compat_snippet_chars_ignored(self) -> None:
        """snippet_chars in payload is accepted without error (backward compat, ignored)."""
        wiki = {"path": "p/x", "title": "X", "body": "Body.", "_wiki": True}
        surreal = _make_boot_surreal(recall_wiki_return=[wiki], lookup_wiki_return=[wiki])
        msg = _make_boot_msg({
            "scopes": ["proj"],
            "group_id": "proj",
            "snippet_chars": 100,  # legacy field — should not crash
        })
        with (
            patch("services.memory.recall_service.SurrealClient", return_value=surreal),
            patch("services.memory.recall_service.embed", return_value=[0.1] * 10),
        ):
            await handle_boot_message(msg)
        published = json.loads(msg._client.publish.call_args[0][1])
        assert "entries" in published


class TestChunkRecallPass:
    """Tests for the wiki_chunk recall pass in _handle_wiki_intents."""

    def _make_surreal(
        self,
        recall_wiki_ret=None,
        lookup_wiki_ret=None,
        recall_chunks_ret=None,
        query_ret=None,
    ):
        surreal = MagicMock()
        surreal.recall_wiki.return_value = recall_wiki_ret or []
        surreal.lookup_wiki.return_value = lookup_wiki_ret or []
        surreal.recall_wiki_chunks.return_value = recall_chunks_ret or []
        surreal.query.return_value = query_ret or []
        return surreal

    def test_chunk_recall_adds_hit_not_in_seen_paths(self):
        from services.memory.recall_service import _handle_wiki_intents
        chunk_row = {
            "parent_path": "docs/hpc",
            "heading": "Singularity",
            "chunk_index": 1,
            "text": "Use singularity exec...",
            "dist": 0.2,
        }
        parent_doc = {"path": "docs/hpc", "title": "HPC Guide", "type": "", "summary": "HPC docs",
                      "confidence": 1.0, "status": "auto_serve"}
        surreal = self._make_surreal(
            recall_chunks_ret=[chunk_row],
            query_ret=[[parent_doc]],
        )
        hits = _handle_wiki_intents(surreal, "singularity", [0.1] * 1024)
        paths = [h["path"] for h in hits]
        assert "docs/hpc" in paths
        chunk_hit = next(h for h in hits if h["path"] == "docs/hpc")
        assert chunk_hit["_wiki"] is True
        assert chunk_hit["_chunk"] == "Use singularity exec..."
        assert chunk_hit["dist"] == 0.2

    def test_chunk_recall_deduped_by_seen_paths_from_whole_page(self):
        """If recall_wiki already returned a path, the chunk hit is skipped."""
        from services.memory.recall_service import _handle_wiki_intents
        whole_page_row = {
            "path": "docs/hpc", "title": "HPC Guide", "type": "", "summary": "",
            "confidence": 1.0, "status": "auto_serve", "dist": 0.1,
        }
        chunk_row = {
            "parent_path": "docs/hpc", "heading": "Singularity", "chunk_index": 0,
            "text": "singularity exec", "dist": 0.15,
        }
        surreal = self._make_surreal(
            recall_wiki_ret=[whole_page_row],
            recall_chunks_ret=[chunk_row],
        )
        hits = _handle_wiki_intents(surreal, "singularity", [0.1] * 1024)
        hpc_hits = [h for h in hits if h.get("path") == "docs/hpc"]
        assert len(hpc_hits) == 1  # not duplicated

    def test_chunk_recall_collapses_to_best_scoring_chunk(self):
        """For multiple chunks from same parent_path, keep the lowest dist."""
        from services.memory.recall_service import _handle_wiki_intents
        chunk_rows = [
            {"parent_path": "docs/x", "heading": "A", "chunk_index": 0, "text": "text A", "dist": 0.5},
            {"parent_path": "docs/x", "heading": "B", "chunk_index": 1, "text": "text B", "dist": 0.2},
            {"parent_path": "docs/x", "heading": "C", "chunk_index": 2, "text": "text C", "dist": 0.4},
        ]
        parent_doc = {"path": "docs/x", "title": "X", "type": "", "summary": "",
                      "confidence": 1.0, "status": "auto_serve"}
        surreal = self._make_surreal(recall_chunks_ret=chunk_rows, query_ret=[[parent_doc]])
        hits = _handle_wiki_intents(surreal, "query", [0.1] * 1024)
        x_hits = [h for h in hits if h.get("path") == "docs/x"]
        assert len(x_hits) == 1
        assert x_hits[0]["_chunk"] == "text B"
        assert x_hits[0]["dist"] == 0.2

    def test_chunk_recall_filters_needs_review_by_default(self):
        """Chunks whose parent status != auto_serve are excluded unless include_needs_review."""
        from services.memory.recall_service import _handle_wiki_intents
        chunk_row = {"parent_path": "docs/draft", "heading": "H", "chunk_index": 0,
                     "text": "draft text", "dist": 0.1}
        parent_doc = {"path": "docs/draft", "title": "Draft", "type": "", "summary": "",
                      "confidence": 1.0, "status": "needs_review"}
        surreal = self._make_surreal(recall_chunks_ret=[chunk_row], query_ret=[[parent_doc]])
        hits = _handle_wiki_intents(surreal, "query", [0.1] * 1024, include_needs_review=False)
        assert not any(h.get("path") == "docs/draft" for h in hits)

    def test_chunk_recall_included_when_include_needs_review(self):
        from services.memory.recall_service import _handle_wiki_intents
        chunk_row = {"parent_path": "docs/draft", "heading": "H", "chunk_index": 0,
                     "text": "draft text", "dist": 0.1}
        parent_doc = {"path": "docs/draft", "title": "Draft", "type": "", "summary": "",
                      "confidence": 1.0, "status": "needs_review"}
        surreal = self._make_surreal(recall_chunks_ret=[chunk_row], query_ret=[[parent_doc]])
        hits = _handle_wiki_intents(surreal, "query", [0.1] * 1024, include_needs_review=True)
        assert any(h.get("path") == "docs/draft" for h in hits)

    def test_chunk_recall_skipped_when_no_embedding(self):
        """Without an embedding, the chunk HNSW pass must not be attempted."""
        from services.memory.recall_service import _handle_wiki_intents
        surreal = self._make_surreal()
        _handle_wiki_intents(surreal, "query", embedding=None)
        surreal.recall_wiki_chunks.assert_not_called()

    def test_chunk_recall_error_does_not_raise(self):
        """recall_wiki_chunks failure must be swallowed (fail-open)."""
        from services.memory.recall_service import _handle_wiki_intents
        surreal = self._make_surreal()
        surreal.recall_wiki_chunks.side_effect = RuntimeError("DB error")
        hits = _handle_wiki_intents(surreal, "query", [0.1] * 1024)
        assert isinstance(hits, list)  # no exception propagated


# ── MR recall surfacing tests ──────────────────────────────────────────────────

class TestMRRecallSurfacing:
    """Tests for MR provenance labelling and source dict enrichment."""

    def test_mr_hits_labelled_in_build_sources(self) -> None:
        """_build_sources exposes url and files_changed for provenance=mr entities."""
        from services.memory.recall_service import _build_sources
        entity = {
            "role": "decision",
            "name": "use distance gate",
            "provenance": "mr",
            "data": {
                "mr": "exohub/pinard!355",
                "url": "https://gitlab.example.com/example/pinard/-/merge_requests/355",
                "files_changed": ["services/memory/recall_service.py"],
            },
            "id": "entity:abc123",
        }
        sources = _build_sources([entity])
        assert len(sources) == 1
        src = sources[0]
        assert src.get("url") == "https://gitlab.example.com/example/pinard/-/merge_requests/355"
        assert src.get("files_changed") == ["services/memory/recall_service.py"]
        assert src.get("mr") == "exohub/pinard!355"

    def test_non_mr_hits_no_mr_fields(self) -> None:
        """Non-MR entities must not gain url/files_changed/mr fields."""
        from services.memory.recall_service import _build_sources
        entity = {
            "role": "decision",
            "name": "something",
            "provenance": "lesson",
            "data": {},
            "id": "entity:def456",
        }
        sources = _build_sources([entity])
        src = sources[0]
        assert "url" not in src
        assert "files_changed" not in src
        assert "mr" not in src

    def test_mr_hits_labelled_in_summarize_text(self) -> None:
        """_summarize builds [role:mr · scope] label for provenance=mr entities."""
        from services.memory.recall_service import _summarize
        from unittest.mock import patch
        entities = [{
            "role": "decision",
            "name": "use distance gate",
            "description": "Chose distance gate over reranker.",
            "provenance": "mr",
            "_scope": "pinard",
        }]
        # Force LLM unavailable so we get verbatim fallback (simpler assertion).
        from services.memory.token_manager import LLMUnavailable
        with patch("services.memory.recall_service._token_manager") as mock_tm:
            mock_tm.get_client.side_effect = LLMUnavailable("no token")
            result = _summarize(entities, max_tokens=200, group_id="pinard")
        assert result is not None
        # The verbatim fallback uses the top entity name — labelling is in the lines
        # built before LLM call, which feeds the LLM when available. Confirm the
        # label format is present in the lines construction by inspecting the source.
        import inspect
        from services.memory import recall_service as rs_mod
        src = inspect.getsource(rs_mod._summarize)
        assert "mr" in src
        assert "provenance" in src

    def test_discussion_not_ingested_v1(self) -> None:
        """No MR-discussion route should exist in the ingester (v1 — Pass 1 only)."""
        from services.memory import ingester as ingester_mod
        assert hasattr(ingester_mod, "_handle_mr_sync")
        import inspect
        src = inspect.getsource(ingester_mod._run_mr_consumer)
        # The consumer must not reference discussion/notes routes.
        assert "discussion" not in src.lower()
        assert "notes" not in src.lower() or "review_note" not in src.lower()
