"""Integration tests for the curated ingester service.

Tests:
1. Engram observation → SurrealDB record+vector upsert (mocked Rosetta + SurrealDB)
2. Teaching episode → extracted entities (mocked LLM extraction)
3. LLM-outage → queue → restore → drain (status file verification)

All external I/O (Rosetta, SurrealDB, NATS, Anthropic) is mocked.
"""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure the repo root (services/) is importable when running from repo root.
# pinard_core is expected to be installed (pip install -e packages/pinard-core
# for in-repo dev, or pinard-core>=1.0 from the internal PyPI in prod/CI).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.engram_reader import EngramObservation, EngramReader
from services.memory.ingester import (
    _Status,
    _extract_entities_from_episode,
    _observation_to_entity,
    ingest_observation,
)
from services.memory.ontology.registry import OntologyRegistry
from services.memory.token_manager import LLMUnavailable, TokenManager


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def registry() -> OntologyRegistry:
    return OntologyRegistry()


@pytest.fixture
def mock_surreal() -> MagicMock:
    surreal = MagicMock()
    surreal.__enter__ = MagicMock(return_value=surreal)
    surreal.__exit__ = MagicMock(return_value=False)
    surreal.upsert_entity = MagicMock(return_value={"id": "entity:test"})
    surreal.relate = MagicMock(return_value={})
    return surreal


@pytest.fixture
def sample_observation() -> EngramObservation:
    return EngramObservation(
        obs_id="obs-001",
        session_id="genomics-step5-abc",
        group_id="genomics-build",
        obs_type="diagnosis",
        content="OOM killed process on shard 47 — memory budget exceeded",
        timestamp=datetime.now(tz=timezone.utc),
        confidence=0.95,
    )


# ── Test 1: Engram observation → SurrealDB upsert ────────────────────────────

class TestObservationIngestion:
    def test_observation_to_entity_mapping_diagnosis(self, registry: OntologyRegistry) -> None:
        role, name, desc = _observation_to_entity(
            "OOM killed process",
            "diagnosis",
            registry,
            "genomics-build",
        )
        assert role == "diagnosis"
        assert "OOM killed process" in name
        assert desc == "OOM killed process"

    def test_observation_to_entity_mapping_rule(self, registry: OntologyRegistry) -> None:
        role, name, desc = _observation_to_entity(
            "always use fix: prefix for commits",
            "rule",
            registry,
            "genomics-build",
        )
        assert role == "decision"

    def test_observation_to_entity_mapping_unknown_type(self, registry: OntologyRegistry) -> None:
        role, name, desc = _observation_to_entity(
            "some unknown observation",
            "unknown_type",
            registry,
            "genomics-build",
        )
        # Unknown types fall back to "artifact"
        assert role == "artifact"

    def test_observation_to_entity_name_truncated(self, registry: OntologyRegistry) -> None:
        long_content = "x" * 200
        role, name, desc = _observation_to_entity(long_content, "fact", registry, "grp")
        assert len(name) <= 120

    def test_observation_to_entity_mapping_bugfix(self, registry: OntologyRegistry) -> None:
        """mem_save 'bugfix' type maps to 'diagnosis' (bug = a diagnosed problem)."""
        role, name, desc = _observation_to_entity(
            "Fixed null pointer in auth handler",
            "bugfix",
            registry,
            "genomics-build",
        )
        assert role == "diagnosis"

    def test_observation_to_entity_mapping_decision(self, registry: OntologyRegistry) -> None:
        """mem_save 'decision' type maps to 'decision'."""
        role, name, desc = _observation_to_entity(
            "Use SurrealDB for memory storage",
            "decision",
            registry,
            "genomics-build",
        )
        assert role == "decision"

    def test_observation_to_entity_mapping_pattern(self, registry: OntologyRegistry) -> None:
        """mem_save 'pattern' type maps to 'decision' (architectural decision)."""
        role, name, desc = _observation_to_entity(
            "Always use JetStream publish for stream subjects",
            "pattern",
            registry,
            "genomics-build",
        )
        assert role == "decision"

    def test_observation_to_entity_mapping_architecture(self, registry: OntologyRegistry) -> None:
        """mem_save 'architecture' type maps to 'artifact'."""
        role, name, desc = _observation_to_entity(
            "Three-tier orchestration: regisseur + maitres + workers",
            "architecture",
            registry,
            "genomics-build",
        )
        assert role == "artifact"

    def test_observation_to_entity_mapping_discovery(self, registry: OntologyRegistry) -> None:
        """mem_save 'discovery' type maps to 'artifact'."""
        role, name, desc = _observation_to_entity(
            "SurrealDB 3.x requires TYPE object FLEXIBLE syntax",
            "discovery",
            registry,
            "genomics-build",
        )
        assert role == "artifact"

    def test_observation_to_entity_name_uses_first_line(self, registry: OntologyRegistry) -> None:
        """Name extraction uses the first non-empty line, not raw 120-char truncation."""
        content = "Short title\nLong body spanning\nmultiple lines with much more content here."
        role, name, desc = _observation_to_entity(content, "fact", registry, "grp")
        assert name == "Short title"

    def test_observation_to_entity_name_falls_back_to_truncation(self, registry: OntologyRegistry) -> None:
        """When content has no newlines, name falls back to 120-char truncation."""
        long_single_line = "z" * 200
        role, name, desc = _observation_to_entity(long_single_line, "fact", registry, "grp")
        assert len(name) <= 120

    def test_session_summary_maps_to_task(self, registry: OntologyRegistry) -> None:
        """mem_save 'session_summary' type maps to 'task' (lifecycle meta, not domain knowledge)."""
        role, name, desc = _observation_to_entity(
            "Session summary for pinard-swe-147",
            "session_summary",
            registry,
            "pinard",
        )
        assert role == "task"

    def test_plan_maps_to_task(self, registry: OntologyRegistry) -> None:
        """mem_save 'plan' type maps to 'task' (actionable intent)."""
        role, name, desc = _observation_to_entity(
            "Plan: implement type_map fix and reingest",
            "plan",
            registry,
            "pinard",
        )
        assert role == "task"

    def test_manual_maps_to_decision(self, registry: OntologyRegistry) -> None:
        """mem_save 'manual' type maps to 'decision' (user-authored guidance)."""
        role, name, desc = _observation_to_entity(
            "Always run typecheck before committing pi-extension changes",
            "manual",
            registry,
            "pinard",
        )
        assert role == "decision"

    def test_title_strips_markdown_heading_prefix(self, registry: OntologyRegistry) -> None:
        """Names like '## Goal' have the heading markers stripped → 'Goal'."""
        content = "## Goal\nThis is the body of the observation."
        role, name, desc = _observation_to_entity(content, "fact", registry, "grp")
        assert name == "Goal"
        assert not name.startswith("#")

    def test_title_strips_single_hash_heading(self, registry: OntologyRegistry) -> None:
        """Names like '# My Title' have the heading marker stripped → 'My Title'."""
        content = "# My Title\nDetails follow here."
        role, name, desc = _observation_to_entity(content, "fact", registry, "grp")
        assert name == "My Title"
        assert not name.startswith("#")

    def test_ingest_observation_calls_embed_and_upsert(
        self, registry: OntologyRegistry, mock_surreal: MagicMock, sample_observation: EngramObservation
    ) -> None:
        with patch("services.memory.ingester.embed") as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            ingest_observation(
                obs_content=sample_observation.content,
                obs_type=sample_observation.obs_type,
                group_id=sample_observation.group_id,
                registry=registry,
                surreal=mock_surreal,
            )
        mock_embed.assert_called_once_with(sample_observation.content)
        mock_surreal.upsert_entity.assert_called_once()
        call_kwargs = mock_surreal.upsert_entity.call_args
        assert call_kwargs.kwargs["role"] == "diagnosis"
        assert call_kwargs.kwargs["embedding"] == [0.1] * 1024
        assert call_kwargs.kwargs["provenance"] == "engram_pg"

    def test_ingest_observation_sets_engram_pg_provenance(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        """ingest_observation must tag directly-ingested entities with provenance='engram_pg'."""
        with patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            ingest_observation(
                obs_content="Fix: corrected the NATS subject parser",
                obs_type="bugfix",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )
        mock_surreal.upsert_entity.assert_called_once()
        call_kwargs = mock_surreal.upsert_entity.call_args
        assert call_kwargs.kwargs.get("provenance") == "engram_pg"

    def test_ingest_observation_continues_on_embedding_error(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        from services.memory.embeddings import EmbeddingError
        with patch("services.memory.ingester.embed", side_effect=EmbeddingError("Rosetta down")):
            # Should not raise — embedding failure is non-fatal.
            ingest_observation(
                obs_content="some content",
                obs_type="fact",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )
        # Upsert still called, but embedding=None.
        mock_surreal.upsert_entity.assert_called_once()
        call_kwargs = mock_surreal.upsert_entity.call_args
        assert call_kwargs.kwargs["embedding"] is None

    def test_ingest_observation_skips_empty_content(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        """Observations with empty content should be skipped gracefully."""
        # ingest_observation is not responsible for filtering empty content;
        # the calling loop does. But empty content should produce a harmless upsert.
        with patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            ingest_observation(
                obs_content="",
                obs_type="fact",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )
        mock_surreal.upsert_entity.assert_called_once()


# ── Test 2: Teaching episode → entity extraction ──────────────────────────────

class TestTeachingEpisodeExtraction:
    def _make_llm_client(self, response_json: dict) -> MagicMock:
        """Return a mock LLMClient whose complete() returns the JSON string."""
        client = MagicMock()
        client.complete.return_value = json.dumps(response_json)
        return client

    def test_normal_mode_extracts_entities(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        llm_response = {
            "entities": [
                {"role": "diagnosis", "name": "OOM on shard 47", "description": "memory exceeded"},
                {"role": "action", "name": "increase --mem-budget", "description": "fix for OOM"},
            ],
            "edges": [
                {
                    "from_role": "diagnosis", "from_name": "OOM on shard 47",
                    "relation": "resolved_by",
                    "to_role": "action", "to_name": "increase --mem-budget",
                    "description": "standard fix",
                }
            ],
        }
        llm_client = self._make_llm_client(llm_response)

        with patch("services.memory.ingester.embed", return_value=[0.5] * 1024):
            n_entities, n_edges = _extract_entities_from_episode(
                llm_client=llm_client,
                episode_content="The TileDB step OOMed on shard 47.",
                mode="normal",
                group_id="genomics-build",
                registry=registry,
                surreal=mock_surreal,
            )

        assert n_entities == 2
        assert n_edges == 1
        assert mock_surreal.upsert_entity.call_count == 2
        assert mock_surreal.relate.call_count == 1
        # All episode-extracted entities must be tagged with episode_extraction provenance.
        for call in mock_surreal.upsert_entity.call_args_list:
            assert call.kwargs.get("provenance") == "episode_extraction"

    def test_teaching_mode_includes_hint_in_prompt(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        llm_response = {"entities": [], "edges": []}
        llm_client = self._make_llm_client(llm_response)

        with patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            _extract_entities_from_episode(
                llm_client=llm_client,
                episode_content="Teaching session content",
                mode="teaching",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )

        # Teaching hint should appear in the prompt.
        call_args = llm_client.complete.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert "teaching session" in prompt.lower() or "teaching" in prompt

    def test_malformed_llm_response_returns_zero(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        llm_client = MagicMock()
        llm_client.complete.return_value = "No JSON here at all"

        with patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            n_entities, n_edges = _extract_entities_from_episode(
                llm_client=llm_client,
                episode_content="some content",
                mode="normal",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )

        assert n_entities == 0
        assert n_edges == 0
        mock_surreal.upsert_entity.assert_not_called()

    def test_extraction_skips_invalid_edges(
        self, registry: OntologyRegistry, mock_surreal: MagicMock
    ) -> None:
        from services.memory.surrealdb.client import SurrealError

        llm_response = {
            "entities": [{"role": "task", "name": "step5", "description": ""}],
            "edges": [
                {
                    "from_role": "task", "from_name": "step5",
                    "relation": "",  # empty relation should be skipped
                    "to_role": "artifact", "to_name": "out.parquet",
                    "description": "",
                }
            ],
        }
        llm_client = self._make_llm_client(llm_response)
        # relate won't be called since relation is empty, but guard against SurrealError too
        mock_surreal.relate.side_effect = SurrealError("entity not found")

        with patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            n_entities, n_edges = _extract_entities_from_episode(
                llm_client=llm_client,
                episode_content="step5 produces output",
                mode="normal",
                group_id="grp",
                registry=registry,
                surreal=mock_surreal,
            )

        # Entity should be upserted; edge silently skipped on error or empty relation.
        assert n_entities == 1


# ── Test 3: LLM-outage → polling sleep → restore → drain ─────────────────────

class TestLLMOutageHandling:
    def _make_manager(self) -> TokenManager:
        from services.memory.llm_client import LLMClient, _StaticKeyProvider
        llm = LLMClient(
            api="anthropic-messages",
            model="claude-haiku",
            token_provider=_StaticKeyProvider("fake-key"),
        )
        return TokenManager(llm_client=llm)

    def test_token_manager_raises_on_expired_token(self) -> None:
        from services.memory.llm_client import LLMAuthError
        manager = self._make_manager()
        with patch.object(manager._llm_client, "probe", side_effect=LLMAuthError("401")):
            with pytest.raises(LLMUnavailable):
                manager.get_client()

    def test_token_manager_succeeds_on_valid_token(self) -> None:
        manager = self._make_manager()
        with patch.object(manager._llm_client, "probe"):
            client = manager.get_client()
        assert client is manager._llm_client
        assert manager._probe_failures == 0

    def test_backoff_schedule_escalates(self) -> None:
        manager = TokenManager()
        delays = [manager.backoff_delay() for _ in range(4)]
        # First delay = 5m, second = 10m, third+ = 30m
        assert delays[0] == 5 * 60
        assert delays[1] == 10 * 60
        assert delays[2] == 30 * 60
        assert delays[3] == 30 * 60  # stays at max

    def test_reset_failures_resets_backoff(self) -> None:
        manager = TokenManager()
        manager.backoff_delay()
        manager.backoff_delay()
        manager.reset_failures()
        # After reset, backoff starts from beginning.
        assert manager.backoff_delay() == 5 * 60

    def test_status_tracks_llm_state(self, tmp_path: Path) -> None:
        import services.memory.ingester as ingester_mod
        orig_status_file = ingester_mod.STATUS_FILE
        ingester_mod.STATUS_FILE = tmp_path / "memory-ingester-status.json"
        try:
            status = _Status()
            status.llm_available = False
            status.state = "waiting"
            status.queue_depth = 5
            status.oldest_pending_age_hours = 2.5
            status.write()

            written = json.loads((tmp_path / "memory-ingester-status.json").read_text())
            assert written["state"] == "waiting"
            assert written["llm_available"] is False
            assert written["queue_depth"] == 5
            assert written["oldest_pending_age_hours"] == 2.5
        finally:
            ingester_mod.STATUS_FILE = orig_status_file

    def test_status_restores_after_llm_available(self, tmp_path: Path) -> None:
        import services.memory.ingester as ingester_mod
        orig_status_file = ingester_mod.STATUS_FILE
        ingester_mod.STATUS_FILE = tmp_path / "memory-ingester-status.json"
        try:
            status = _Status()
            # Simulate: LLM comes back online.
            status.llm_available = True
            status.state = "draining"
            status.extracted_today = 3
            status.write()

            written = json.loads((tmp_path / "memory-ingester-status.json").read_text())
            assert written["state"] == "draining"
            assert written["llm_available"] is True
            assert written["extracted_today"] == 3
        finally:
            ingester_mod.STATUS_FILE = orig_status_file

    def test_status_has_all_required_fields(self, tmp_path: Path) -> None:
        import services.memory.ingester as ingester_mod
        orig_status_file = ingester_mod.STATUS_FILE
        ingester_mod.STATUS_FILE = tmp_path / "status.json"
        try:
            status = _Status()
            status.write()
            written = json.loads((tmp_path / "status.json").read_text())
            required = {
                "state", "llm_available", "queue_depth", "last_extraction_at",
                "extracted_today", "errors_today", "oldest_pending_age_hours",
            }
            assert required.issubset(set(written.keys()))
        finally:
            ingester_mod.STATUS_FILE = orig_status_file


# ── Test 4: EngramReader ───────────────────────────────────────────────────────

class TestEngramReader:
    def test_fetch_raises_on_404(self) -> None:
        """404 means wrong endpoint path, not empty results (live Engram returns 200+[] for unknown projects)."""
        from services.memory.engram_reader import EngramReaderError
        reader = EngramReader(group_id="my-group", url="http://engram-test")
        mock_resp = MagicMock(status_code=404)
        mock_resp.text = "Not Found"
        with patch("httpx.get", return_value=mock_resp):
            with pytest.raises(EngramReaderError, match="HTTP 404"):
                reader.fetch()

    def test_fetch_returns_empty_list_on_200_empty(self) -> None:
        """Unknown projects return HTTP 200 with empty list — not 404."""
        reader = EngramReader(group_id="nonexistent-project", url="http://engram-test")
        mock_resp = MagicMock(status_code=200)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = []
        with patch("httpx.get", return_value=mock_resp):
            result = reader.fetch()
        assert result == []

    def test_fetch_raises_on_connection_error(self) -> None:
        """Transient connection errors propagate as EngramReaderError."""
        import httpx
        from services.memory.engram_reader import EngramReaderError
        reader = EngramReader(group_id="my-group", url="http://engram-test")
        with patch("httpx.get", side_effect=httpx.RequestError("connection refused")):
            with pytest.raises(EngramReaderError, match="connection failed"):
                reader.fetch()

    def test_fetch_raises_on_5xx_error(self) -> None:
        """HTTP 5xx surfaces loudly as EngramReaderError (config/endpoint bug)."""
        from services.memory.engram_reader import EngramReaderError
        reader = EngramReader(group_id="my-group", url="http://engram-test")
        mock_resp = MagicMock(status_code=500)
        mock_resp.text = "Internal Server Error"
        with patch("httpx.get", return_value=mock_resp):
            with pytest.raises(EngramReaderError, match="HTTP 500"):
                reader.fetch()

    def test_fetch_uses_correct_endpoint_path(self) -> None:
        """Endpoint must be /observations (no /api prefix — spike-verified)."""
        reader = EngramReader(group_id="genomics-build", url="http://engram-test")
        mock_resp = MagicMock(status_code=200)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = []
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            reader.fetch()
        called_url = mock_get.call_args[0][0]
        assert called_url == "http://engram-test/observations"
        assert "/api/" not in called_url

    def test_fetch_parses_observations(self) -> None:
        reader = EngramReader(group_id="genomics-build", url="http://engram-test")
        api_response = [
            {
                "id": "1",
                "session_id": "sess-abc",
                "project": "genomics-build",
                "type": "diagnosis",
                "content": "OOM on shard 47",
                "created_at": "2026-07-01T10:00:00Z",
                "confidence": 0.9,
            },
            {
                "id": "2",
                "session_id": "sess-abc",
                "project": "genomics-build",
                "type": "action",
                "content": "increase --mem-budget to 64G",
                "created_at": "2026-07-01T10:05:00Z",
                "confidence": 1.0,
            },
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = api_response
        with patch("httpx.get", return_value=mock_resp):
            result = reader.fetch()

        assert len(result) == 2
        assert result[0].obs_type == "diagnosis"
        assert result[0].group_id == "genomics-build"
        assert result[1].obs_type == "action"

    def test_fetch_skips_malformed_items(self) -> None:
        reader = EngramReader(group_id="grp", url="http://engram-test")
        api_response = [
            {"id": "1", "content": "good", "type": "fact"},
            None,  # malformed
            {"content": "also good", "type": "rule"},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = api_response
        with patch("httpx.get", return_value=mock_resp):
            result = reader.fetch()
        # None item should be skipped; good items returned.
        assert len(result) >= 1


# ── Test 5: Query handler ─────────────────────────────────────────────────────

class TestQueryHandler:
    @pytest.mark.asyncio
    async def test_handle_query_no_reply_subject(self) -> None:
        from services.memory.query_handler import handle_query_message

        msg = MagicMock()
        msg.data = json.dumps({"group_id": "grp", "max_facts": 10}).encode()
        msg.reply = None  # No reply subject.

        registry = OntologyRegistry()
        # Should return without error.
        await handle_query_message(msg, registry)

    @pytest.mark.asyncio
    async def test_handle_query_missing_group_id(self) -> None:
        from services.memory.query_handler import handle_query_message

        msg = MagicMock()
        msg.data = json.dumps({"max_facts": 10}).encode()
        msg.reply = "_INBOX.test"

        registry = OntologyRegistry()
        await handle_query_message(msg, registry)
        # No publish should happen — fail-open.

    @pytest.mark.asyncio
    async def test_handle_query_surreal_unavailable(self) -> None:
        from services.memory.query_handler import handle_query_message
        from services.memory.surrealdb.client import SurrealError

        msg = MagicMock()
        msg.data = json.dumps({"group_id": "grp", "max_facts": 5}).encode()
        msg.reply = "_INBOX.test"

        registry = OntologyRegistry()
        with patch(
            "services.memory.query_handler.SurrealClient",
            side_effect=SurrealError("connection refused"),
        ):
            # Should not raise — fail-open.
            await handle_query_message(msg, registry)

    @pytest.mark.asyncio
    async def test_handle_query_returns_recipe_content(self) -> None:
        from services.memory.query_handler import handle_query_message

        msg = MagicMock()
        msg.data = json.dumps({"group_id": "genomics-build", "max_facts": 5}).encode()
        msg.reply = "_INBOX.test"

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.recall.return_value = [
            {"role": "diagnosis", "name": "OOM on shard 47", "description": "memory exceeded", "data": {}}
        ]
        mock_surreal.lookup.return_value = []
        mock_surreal.trace.return_value = []

        published: list[bytes] = []

        async def fake_publish(subject: str, data: bytes) -> None:
            published.append(data)

        msg._client = MagicMock()
        msg._client.publish = fake_publish

        registry = OntologyRegistry()
        with (
            patch("services.memory.query_handler.SurrealClient", return_value=mock_surreal),
            patch("services.memory.query_handler.embed", return_value=[0.1] * 1024),
        ):
            await handle_query_message(msg, registry)

        assert len(published) == 1
        response = json.loads(published[0])
        assert response["group_id"] == "genomics-build"
        assert "[pipeline-knowledge]" in response["content"]


# ── Test 6: cursor-advance safety (upsert failure must not advance cursor) ──────

class TestCursorAdvanceSafety:
    """Regression tests: cursor must NOT advance when a connection-level error occurs."""

    def _make_obs(self, seq: int = 1, content: str = "test obs") -> "EngramObservation":
        from datetime import datetime, timezone
        return EngramObservation(
            obs_id=f"obs-{seq}",
            session_id="sess-x",
            group_id="proj-x",
            obs_type="fact",
            content=content,
            timestamp=datetime.now(tz=timezone.utc),
            confidence=1.0,
        )

    def test_connection_level_surreal_error_does_not_advance_cursor(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """A connection-level SurrealError escaping the with-block must NOT advance the cursor."""
        import services.memory.ingester as ingester_mod
        from services.memory.surrealdb.client import SurrealError
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "postgres")

        registry = OntologyRegistry()

        # SurrealClient context manager raises on __enter__
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(side_effect=SurrealError("connection refused"))
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.set_ingest_cursor = MagicMock()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"ENGRAM_PG_DSN": "postgresql://fake"}),
        ):
            ingester_mod._ingest_group("proj-x", registry)

        # set_ingest_cursor (called by SurrealCursorStore.update) must never have been called
        mock_surreal.set_ingest_cursor.assert_not_called()

    def test_advance_cursor_false_does_not_call_cursor_store_update(self) -> None:
        """EngramPostgresReader with advance_cursor=False must not call cursor_store.update."""
        from services.memory.engram_postgres_reader import EngramPostgresReader

        mock_cursor_store = MagicMock()
        mock_cursor_store.get.return_value = 0

        rows = [
            (1, "obs-1", "upsert", {"content": "obs one", "type": "fact"}),
            (2, "obs-2", "upsert", {"content": "obs two", "type": "fact"}),
        ]

        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = rows

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        reader = EngramPostgresReader(
            project="proj-x",
            dsn="postgresql://fake",
            cursor_store=mock_cursor_store,
            advance_cursor=False,
        )

        with patch("psycopg.connect", return_value=mock_conn):
            observations = reader.fetch()

        # Cursor store update must NOT have been called
        mock_cursor_store.update.assert_not_called()
        # max_seq must be set to the highest seq in the batch
        assert reader.max_seq == 2
        # Observations must still be returned
        assert len(observations) == 2

    def test_advance_cursor_true_default_updates_cursor_after_fetch(self) -> None:
        """Default advance_cursor=True must call cursor_store.update after fetch."""
        from services.memory.engram_postgres_reader import EngramPostgresReader

        mock_cursor_store = MagicMock()
        mock_cursor_store.get.return_value = 0

        rows = [
            (3, "obs-3", "upsert", {"content": "obs three", "type": "fact"}),
        ]

        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = rows

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        reader = EngramPostgresReader(
            project="proj-x",
            dsn="postgresql://fake",
            cursor_store=mock_cursor_store,
            advance_cursor=True,
        )

        with patch("psycopg.connect", return_value=mock_conn):
            reader.fetch()

        mock_cursor_store.update.assert_called_once_with("proj-x", 3)

    def test_single_surreal_client_on_postgres_path(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """The postgres path must open exactly ONE SurrealClient per _ingest_group call."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "postgres")

        registry = OntologyRegistry()
        obs = self._make_obs()

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.ensure_schema = MagicMock()
        mock_surreal.get_ingest_cursor = MagicMock(return_value=0)
        mock_surreal.set_ingest_cursor = MagicMock()

        call_count = {"n": 0}

        def counting_surreal(**kwargs):
            call_count["n"] += 1
            return mock_surreal

        with (
            patch("services.memory.ingester.SurrealClient", side_effect=counting_surreal),
            patch("services.memory.ingester.EngramPostgresReader") as mock_reader_cls,
            patch("services.memory.ingester.ingest_observation"),
            patch.dict("os.environ", {"ENGRAM_PG_DSN": "postgresql://fake"}),
        ):
            mock_reader = MagicMock()
            mock_reader.fetch.return_value = [obs]
            mock_reader.max_seq = 1
            mock_reader_cls.return_value = mock_reader

            ingester_mod._ingest_group("proj-x", registry)

        assert call_count["n"] == 1, (
            f"Expected exactly 1 SurrealClient on postgres path, got {call_count['n']}"
        )

# ── Test 7: ensure_schema caching and invocation ──────────────────────────────

class TestEnsureSchema:
    def setup_method(self) -> None:
        # Clear the module-level cache before each test.
        import services.memory.surrealdb.client as client_mod
        client_mod._schema_applied.clear()

    def _make_client(self, group_id: str = "test-group") -> MagicMock:
        from services.memory.surrealdb.client import SurrealClient
        with patch.object(SurrealClient, "__init__", lambda self, **kw: None):
            client = SurrealClient.__new__(SurrealClient)
            client.group_id = group_id
        return client

    def test_ensure_schema_calls_apply_schema_on_first_touch(self) -> None:
        from services.memory.surrealdb.client import SurrealClient, SCHEMA_PATH
        client = self._make_client("grp-a")
        with patch.object(client, "apply_schema") as mock_apply:
            client.ensure_schema()
        mock_apply.assert_called_once_with(str(SCHEMA_PATH))

    def test_ensure_schema_no_op_on_second_call_same_group(self) -> None:
        from services.memory.surrealdb.client import SurrealClient
        client = self._make_client("grp-b")
        with patch.object(client, "apply_schema") as mock_apply:
            client.ensure_schema()
            client.ensure_schema()
        mock_apply.assert_called_once()

    def test_ensure_schema_applies_for_different_groups(self) -> None:
        from services.memory.surrealdb.client import SurrealClient
        client_a = self._make_client("grp-c")
        client_b = self._make_client("grp-d")
        with patch.object(client_a, "apply_schema") as mock_a:
            client_a.ensure_schema()
        with patch.object(client_b, "apply_schema") as mock_b:
            client_b.ensure_schema()
        mock_a.assert_called_once()
        mock_b.assert_called_once()

    def test_ensure_schema_accepts_custom_path(self) -> None:
        from services.memory.surrealdb.client import SurrealClient
        client = self._make_client("grp-e")
        custom_path = "/tmp/custom.surql"
        with patch.object(client, "apply_schema") as mock_apply:
            client.ensure_schema(custom_path)
        mock_apply.assert_called_once_with(custom_path)

    def test_run_engram_ingestion_calls_ensure_schema_before_upsert(self) -> None:
        """ensure_schema is called before any upsert_entity in the ingestion loop."""
        from services.memory.ingester import _run_engram_ingestion
        from services.memory.engram_reader import EngramObservation
        from services.memory.ontology.registry import OntologyRegistry
        from datetime import datetime, timezone

        registry = OntologyRegistry()
        obs = EngramObservation(
            obs_id="o1",
            session_id="s1",
            group_id="grp-f",
            obs_type="fact",
            content="some fact",
            timestamp=datetime.now(tz=timezone.utc),
            confidence=1.0,
        )

        call_order: list[str] = []

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.ensure_schema.side_effect = lambda *a, **kw: call_order.append("ensure_schema")
        mock_surreal.upsert_entity.side_effect = lambda **kw: call_order.append("upsert_entity")

        with patch("services.memory.ingester.EngramReader") as mock_reader_cls:
            with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal):
                with patch("services.memory.ingester.embed", return_value=[0.1] * 1024):
                    with patch.dict("os.environ", {"WORKER_GROUP_ID": "grp-f"}):
                        mock_reader = MagicMock()
                        mock_reader.fetch.return_value = [obs]
                        mock_reader_cls.return_value = mock_reader
                        _run_engram_ingestion(registry)

        assert "ensure_schema" in call_order
        assert "upsert_entity" in call_order
        assert call_order.index("ensure_schema") < call_order.index("upsert_entity")


# ── Test 8: schema.surql content validation ───────────────────────────────────

class TestSchemaContent:
    """Validates schema.surql syntax and structure without a live SurrealDB."""

    def _load_schema(self) -> str:
        from services.memory.surrealdb.client import SCHEMA_PATH
        return SCHEMA_PATH.read_text()

    def test_schema_file_exists(self) -> None:
        from services.memory.surrealdb.client import SCHEMA_PATH
        assert SCHEMA_PATH.exists(), f"schema.surql not found at {SCHEMA_PATH}"

    def test_schema_uses_3x_flexible_syntax(self) -> None:
        """All FLEXIBLE fields must use 'TYPE object FLEXIBLE' (SurrealDB 3.x syntax).

        'FLEXIBLE TYPE object' (2.x syntax) causes a parse error on 3.x and
        aborts the schema apply partway, leaving no indexes.
        """
        schema = self._load_schema()
        assert "FLEXIBLE TYPE object" not in schema, (
            "schema.surql contains 2.x syntax 'FLEXIBLE TYPE object'; "
            "must be 'TYPE object FLEXIBLE' for SurrealDB 3.x"
        )

    def test_schema_defines_hnsw_index(self) -> None:
        schema = self._load_schema()
        assert "entity_embedding_hnsw" in schema
        assert "HNSW DIMENSION 1024" in schema

    def test_schema_defines_fts_indexes(self) -> None:
        """SurrealDB 3.x requires one FULLTEXT index per field (no multi-field)."""
        import re
        schema = self._load_schema()
        # entity: two single-field FTS indexes
        assert "entity_name_fts" in schema
        assert "entity_description_fts" in schema
        # wiki_doc: two single-field FTS indexes
        assert "wiki_doc_title_fts" in schema
        assert "wiki_doc_body_fts" in schema
        assert "FULLTEXT ANALYZER pinard_text BM25" in schema
        # Guard against re-introducing multi-field FULLTEXT (parse error on 3.x).
        # Match any DEFINE INDEX block that has >1 field and FULLTEXT.
        multi_fts = re.search(
            r"DEFINE INDEX[^;]*FIELDS\s+\w+\s*,\s*\w+[^;]*FULLTEXT",
            schema,
            re.DOTALL,
        )
        assert multi_fts is None, (
            "schema.surql contains a multi-field FULLTEXT index (invalid on SurrealDB 3.x): "
            + (multi_fts.group(0) if multi_fts else "")
        )

    def test_schema_defines_wiki_doc_table(self) -> None:
        schema = self._load_schema()
        assert "DEFINE TABLE IF NOT EXISTS wiki_doc" in schema

    def test_schema_defines_all_edge_tables(self) -> None:
        schema = self._load_schema()
        for table in [
            "depends_on", "produces", "consumes", "indicates_problem",
            "resolved_by", "requires_condition", "triggers_decision",
            "wiki_references", "wiki_mentions",
        ]:
            assert f"DEFINE TABLE IF NOT EXISTS {table}" in schema, (
                f"Edge table '{table}' missing from schema.surql"
            )


# ── Test 9: asyncio.to_thread usage in event-loop loops ──────────────────────

class TestToThreadOffload:
    """Verify that _engram_loop and _rollup_loop use asyncio.to_thread for blocking work.

    Event-loop starvation (NATS Authorization Violation) is caused by running
    synchronous blocking work directly on the event loop. These tests guard
    against regressions where the to_thread wrapping is accidentally removed.
    """

    def test_engram_loop_uses_to_thread(self) -> None:
        """Structural check: _engram_loop passes _run_engram_ingestion to asyncio.to_thread."""
        import services.memory.ingester as ingester_mod
        import inspect

        source = inspect.getsource(ingester_mod.run)
        assert "asyncio.to_thread(_run_engram_ingestion" in source, (
            "_engram_loop must use asyncio.to_thread(_run_engram_ingestion, ...) "
            "not call _run_engram_ingestion() directly — direct calls block the event loop "
            "and cause NATS Authorization Violation on reconnect"
        )

    @pytest.mark.asyncio
    async def test_engram_loop_body_calls_to_thread(self) -> None:
        """Structural check: asyncio.to_thread is called with _run_engram_ingestion in _engram_loop."""
        import services.memory.ingester as ingester_mod
        import inspect

        source = inspect.getsource(ingester_mod.run)
        # The loop body must contain 'asyncio.to_thread(_run_engram_ingestion'
        assert "asyncio.to_thread(_run_engram_ingestion" in source, (
            "_engram_loop must use asyncio.to_thread(_run_engram_ingestion, ...) "
            "to avoid blocking the event loop"
        )

    @pytest.mark.asyncio
    async def test_rollup_loop_body_calls_to_thread_for_rollup(self) -> None:
        """Structural check: asyncio.to_thread is used for the rollup engine.run() call."""
        import services.memory.ingester as ingester_mod
        import inspect

        source = inspect.getsource(ingester_mod.run)
        assert "asyncio.to_thread(_do_rollup)" in source, (
            "_rollup_loop must use asyncio.to_thread(_do_rollup) to avoid blocking the event loop"
        )

    @pytest.mark.asyncio
    async def test_rollup_loop_body_calls_to_thread_for_promotion(self) -> None:
        """Structural check: asyncio.to_thread is used for the promotion detection block."""
        import services.memory.ingester as ingester_mod
        import inspect

        source = inspect.getsource(ingester_mod.run)
        assert "asyncio.to_thread(_do_promotion)" in source, (
            "_rollup_loop must use asyncio.to_thread(_do_promotion) to avoid blocking the event loop"
        )

    def test_run_engram_ingestion_is_sync_function(self) -> None:
        """_run_engram_ingestion must be a plain def (not async) — it runs in a thread."""
        import inspect
        import services.memory.ingester as ingester_mod

        assert not inspect.iscoroutinefunction(ingester_mod._run_engram_ingestion), (
            "_run_engram_ingestion must remain a plain synchronous function "
            "so it can safely run in asyncio.to_thread"
        )

    def test_process_episode_message_uses_to_thread(self) -> None:
        """Structural check: _process_episode_message delegates sync work to asyncio.to_thread."""
        import inspect
        import services.memory.ingester as ingester_mod

        source = inspect.getsource(ingester_mod._process_episode_message)
        assert "asyncio.to_thread" in source, (
            "_process_episode_message must use asyncio.to_thread for the sync processing body "
            "(LLM completion + embed + SurrealDB upserts) to avoid blocking the event loop "
            "and causing NATS Authorization Violation"
        )

    def test_do_process_episode_sync_is_sync_function(self) -> None:
        """_do_process_episode_sync must be a plain def, not async — it runs in a thread."""
        import inspect
        import services.memory.ingester as ingester_mod

        assert hasattr(ingester_mod, "_do_process_episode_sync"), (
            "_do_process_episode_sync must exist in ingester module"
        )
        assert not inspect.iscoroutinefunction(ingester_mod._do_process_episode_sync), (
            "_do_process_episode_sync must be a plain synchronous function "
            "so it can safely run in asyncio.to_thread"
        )

    def test_process_episode_message_calls_to_thread_with_sync_fn(self) -> None:
        """Structural check: asyncio.to_thread is called with _do_process_episode_sync."""
        import inspect
        import services.memory.ingester as ingester_mod

        source = inspect.getsource(ingester_mod._process_episode_message)
        assert "_do_process_episode_sync" in source, (
            "_process_episode_message must call _do_process_episode_sync via asyncio.to_thread"
        )


# ── Test 10: --reingest flag resets cursors ───────────────────────────────────

class TestReingestFlag:
    """Verify _reset_cursors_for_reingest resets all group cursor to seq=0."""

    def test_reingest_resets_all_group_cursors(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        # Override MEMORY_ENGRAM_SOURCE so _resolve_group_ids uses the simple path.
        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.set_ingest_cursor = MagicMock()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "pinard"}),
        ):
            ingester_mod._reset_cursors_for_reingest(registry)

        # set_ingest_cursor must have been called with seq=0 for the group.
        calls = mock_surreal.set_ingest_cursor.call_args_list
        assert len(calls) >= 1
        for c in calls:
            assert c.args[1] == 0 or c.kwargs.get("seq", 0) == 0, (
                f"Expected seq=0 but got: {c}"
            )

    def test_reingest_resets_cursor_with_correct_source_prefix(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """Cursor key must be 'engram_pg:<group_id>' (matches SurrealCursorStore._key)."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.set_ingest_cursor = MagicMock()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "my-project"}),
        ):
            ingester_mod._reset_cursors_for_reingest(registry)

        calls = mock_surreal.set_ingest_cursor.call_args_list
        keys = [c.args[0] for c in calls]
        assert any("my-project" in k for k in keys), (
            f"Expected a cursor key containing 'my-project', got: {keys}"
        )
        assert all("engram_pg" in k for k in keys), (
            f"Expected cursor keys prefixed with 'engram_pg:', got: {keys}"
        )


# ── Test 11: --recurate flag clears wiki_doc + cursors ────────────────────────

class TestRecurateFlag:
    """Verify _recurate_all_scopes clears non-human wiki_doc and drops wiki_curator_cursor."""

    def _make_mock_surreal(self) -> MagicMock:
        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        mock.query = MagicMock(return_value=[[]])
        return mock

    def test_recurate_clears_vignes(self, monkeypatch: "pytest.MonkeyPatch", tmp_path: Path) -> None:
        """DELETE + REMOVE are called for all vignes (per-project group_ids)."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()
        mock_surreal = self._make_mock_surreal()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "test-project", "PINARD_PARCELLE": ""}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
        ):
            ingester_mod._recurate_all_scopes(registry)

        calls = mock_surreal.query.call_args_list
        sql_calls = [c.args[0] for c in calls]
        assert any("DELETE wiki_doc" in s for s in sql_calls), (
            f"Expected a DELETE wiki_doc call, got: {sql_calls}"
        )
        assert any("REMOVE TABLE" in s and "wiki_curator_cursor" in s for s in sql_calls), (
            f"Expected a REMOVE TABLE wiki_curator_cursor call, got: {sql_calls}"
        )

    def test_recurate_includes_vignoble_scopes(
        self, monkeypatch: "pytest.MonkeyPatch", tmp_path: Path
    ) -> None:
        """vignoble-* scopes are opened when VIGNOBLES_BASE_DIR is set."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        # Create a fake vignoble dir with a vignes.yaml.
        vignoble_dir = tmp_path / "vignoble-alpha"
        vignoble_dir.mkdir()
        (vignoble_dir / "vignes.yaml").write_text("vignes:\n  proj-a: {}\n")

        registry = OntologyRegistry()
        opened_group_ids: list[str] = []

        def fake_surreal_cls(group_id: str, **kwargs: object) -> MagicMock:
            opened_group_ids.append(group_id)
            return self._make_mock_surreal()

        with (
            patch("services.memory.ingester.SurrealClient", side_effect=fake_surreal_cls),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "", "PINARD_PARCELLE": ""}),
            patch(
                "services.memory.rollup._get_vignobles_base_dir",
                return_value=str(tmp_path),
            ),
        ):
            ingester_mod._recurate_all_scopes(registry)

        assert "vignoble-alpha" in opened_group_ids, (
            f"Expected 'vignoble-alpha' in opened scopes, got: {opened_group_ids}"
        )

    def test_recurate_always_includes_global(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """__global__ scope is always included regardless of other env vars."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.wiki.curator import GLOBAL_WIKI_GROUP

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()
        opened_group_ids: list[str] = []

        def fake_surreal_cls(group_id: str, **kwargs: object) -> MagicMock:
            opened_group_ids.append(group_id)
            return self._make_mock_surreal()

        with (
            patch("services.memory.ingester.SurrealClient", side_effect=fake_surreal_cls),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "", "PINARD_PARCELLE": ""}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
        ):
            ingester_mod._recurate_all_scopes(registry)

        assert GLOBAL_WIKI_GROUP in opened_group_ids, (
            f"Expected '{GLOBAL_WIKI_GROUP}' in opened scopes, got: {opened_group_ids}"
        )

    def test_recurate_includes_parcelle_scope(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """parcelle-* scope is included when PINARD_PARCELLE is set."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()
        opened_group_ids: list[str] = []

        def fake_surreal_cls(group_id: str, **kwargs: object) -> MagicMock:
            opened_group_ids.append(group_id)
            return self._make_mock_surreal()

        with (
            patch("services.memory.ingester.SurrealClient", side_effect=fake_surreal_cls),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "", "PINARD_PARCELLE": "my-parcelle"}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
        ):
            ingester_mod._recurate_all_scopes(registry)

        assert "parcelle-my-parcelle" in opened_group_ids, (
            f"Expected 'parcelle-my-parcelle' in opened scopes, got: {opened_group_ids}"
        )

    def test_recurate_delete_uses_correct_predicate(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """DELETE predicate must check frontmatter.source (not top-level source)."""
        import services.memory.ingester as ingester_mod
        from services.memory.ontology.registry import OntologyRegistry

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        registry = OntologyRegistry()
        all_sql: list[str] = []
        mock_surreal = self._make_mock_surreal()

        def capture_query(sql: str, *args: object, **kwargs: object) -> list[list[object]]:
            all_sql.append(sql)
            return [[]]

        mock_surreal.query = capture_query

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "grp", "PINARD_PARCELLE": ""}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
        ):
            ingester_mod._recurate_all_scopes(registry)

        delete_calls = [s for s in all_sql if "DELETE wiki_doc" in s]
        assert delete_calls, "No DELETE wiki_doc statement found"
        for stmt in delete_calls:
            assert "frontmatter.source" in stmt, (
                f"Expected 'frontmatter.source' in DELETE predicate, got: {stmt!r}"
            )
            assert "IS NONE" in stmt, (
                f"Expected 'IS NONE' guard in DELETE predicate, got: {stmt!r}"
            )


class TestRechunkFlag:
    """Tests for _rechunk_all_scopes and --rechunk CLI flag."""

    def test_rechunk_iterates_wiki_docs_and_upserts_chunks(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        import services.memory.ingester as ingester_mod

        registry = MagicMock()
        registry.registered_groups.return_value = ["proj-a"]
        registry.compose.return_value = MagicMock()

        pages = [
            {"path": "docs/setup", "title": "Setup Guide", "body": "## Install\n\nRun pip.\n\n## Config\n\nSet env."},
        ]
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.query.return_value = [pages]
        mock_surreal.delete_wiki_chunks_by_path = MagicMock()
        mock_surreal.upsert_wiki_chunks = MagicMock()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "proj-a", "PINARD_PARCELLE": ""}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
            patch("services.memory.ingester.embed", return_value=[0.0] * 1024),
        ):
            ingester_mod._rechunk_all_scopes(registry)

        mock_surreal.delete_wiki_chunks_by_path.assert_called_with("docs/setup")
        assert mock_surreal.upsert_wiki_chunks.called
        chunk_rows = mock_surreal.upsert_wiki_chunks.call_args[0][0]
        assert isinstance(chunk_rows, list)
        assert len(chunk_rows) >= 1
        assert chunk_rows[0]["parent_path"] == "docs/setup"

    def test_rechunk_best_effort_on_page_error(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        """A failure on one page must not abort processing of others."""
        import services.memory.ingester as ingester_mod

        registry = MagicMock()
        registry.registered_groups.return_value = ["proj-a"]
        registry.compose.return_value = MagicMock()

        pages = [
            {"path": "docs/bad", "title": "Bad", "body": "## H\n\ntext"},
            {"path": "docs/good", "title": "Good", "body": "## H\n\ntext"},
        ]
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.query.return_value = [pages]
        call_count = {"n": 0}

        def delete_side_effect(path):
            call_count["n"] += 1
            if path == "docs/bad":
                raise RuntimeError("simulated DB error")

        mock_surreal.delete_wiki_chunks_by_path = MagicMock(side_effect=delete_side_effect)
        mock_surreal.upsert_wiki_chunks = MagicMock()

        with (
            patch("services.memory.ingester.SurrealClient", return_value=mock_surreal),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "proj-a", "PINARD_PARCELLE": ""}),
            patch("services.memory.rollup._get_vignobles_base_dir", return_value=None),
            patch("services.memory.ingester.embed", return_value=[0.0] * 1024),
        ):
            ingester_mod._rechunk_all_scopes(registry)  # must not raise

        # Good page should still be processed.
        good_calls = [
            c for c in mock_surreal.delete_wiki_chunks_by_path.call_args_list
            if c[0][0] == "docs/good"
        ]
        assert good_calls, "docs/good should have been processed despite docs/bad error"


# ── Test: _process_rule_message_sync edit_entity op ──────────────────────────

class TestProcessRuleMessageEditEntity:
    """Tests for the new edit_entity op in _process_rule_message_sync."""

    @pytest.fixture
    def registry(self) -> "OntologyRegistry":
        return OntologyRegistry()

    def _make_surreal_mock(self, existing_entity: dict | None = None) -> "MagicMock":
        from unittest.mock import MagicMock
        surreal = MagicMock()
        surreal.__enter__ = MagicMock(return_value=surreal)
        surreal.__exit__ = MagicMock(return_value=False)
        surreal.fetch_entity_by_id = MagicMock(return_value=existing_entity)
        surreal.update_entity_description = MagicMock(return_value={"id": "entity:abc", "role": "artifact", "name": "test"})
        return surreal

    def test_edit_entity_calls_update_entity_description(self, registry: "OntologyRegistry") -> None:
        from unittest.mock import patch, MagicMock
        from services.memory.ingester import _process_rule_message_sync

        existing = {"id": "entity:abc", "role": "artifact", "name": "test", "provenance": "episode_extraction"}
        surreal = self._make_surreal_mock(existing_entity=existing)

        payload = {
            "op": "edit_entity",
            "entity_id": "entity:abc",
            "content": "Updated description text",
            "project": "exo-cli",
        }

        with patch("services.memory.ingester.SurrealClient", return_value=surreal), \
             patch("services.memory.ingester.embed", return_value=[0.2] * 1024):
            _process_rule_message_sync(payload, registry)

        surreal.fetch_entity_by_id.assert_called_once_with("entity:abc")
        surreal.update_entity_description.assert_called_once_with(
            "entity:abc", "Updated description text", [0.2] * 1024
        )

    def test_edit_entity_missing_entity_logs_warning(self, registry: "OntologyRegistry") -> None:
        from unittest.mock import patch
        from services.memory.ingester import _process_rule_message_sync

        surreal = self._make_surreal_mock(existing_entity=None)
        payload = {
            "op": "edit_entity",
            "entity_id": "entity:missing",
            "content": "new content",
            "project": "exo-cli",
        }

        with patch("services.memory.ingester.SurrealClient", return_value=surreal), \
             patch("services.memory.ingester.embed", return_value=[0.0] * 1024):
            _process_rule_message_sync(payload, registry)

        surreal.update_entity_description.assert_not_called()

    def test_edit_entity_malformed_payload_no_crash(self, registry: "OntologyRegistry") -> None:
        from services.memory.ingester import _process_rule_message_sync

        # Missing required fields — should log a warning and return cleanly
        for payload in [
            {"op": "edit_entity", "content": "text", "project": "proj"},  # no entity_id
            {"op": "edit_entity", "entity_id": "entity:x", "project": "proj"},  # no content
            {"op": "edit_entity", "entity_id": "entity:x", "content": "text"},  # no project
        ]:
            _process_rule_message_sync(payload, registry)  # must not raise

    def test_edit_entity_embedding_failure_proceeds_without_embedding(self, registry: "OntologyRegistry") -> None:
        from unittest.mock import patch
        from services.memory.ingester import _process_rule_message_sync
        from services.memory.embeddings import EmbeddingError

        existing = {"id": "entity:abc", "role": "artifact", "name": "test"}
        surreal = self._make_surreal_mock(existing_entity=existing)
        payload = {
            "op": "edit_entity",
            "entity_id": "entity:abc",
            "content": "text",
            "project": "exo-cli",
        }

        with patch("services.memory.ingester.SurrealClient", return_value=surreal), \
             patch("services.memory.ingester.embed", side_effect=EmbeddingError("timeout")):
            _process_rule_message_sync(payload, registry)

        # Should still call update with None embedding
        surreal.update_entity_description.assert_called_once_with("entity:abc", "text", None)


# ── Test: upsert_entity clobber guard (ingester-level) ───────────────────────

class TestUpsertEntityClobberGuardIngester:
    """Verify that upsert_entity SQL contains the manual_edit conditional guard."""

    def test_schema_gen_base_ddl_has_manual_edit_field(self) -> None:
        from services.memory.surrealdb.schema_gen import _BASE_DDL
        assert "manual_edit" in _BASE_DDL

    def test_schema_surql_has_manual_edit_field(self) -> None:
        from services.memory.surrealdb.client import SCHEMA_PATH
        schema = SCHEMA_PATH.read_text()
        assert "manual_edit" in schema

    def test_upsert_entity_sql_guards_description_and_embedding(self) -> None:
        """The upsert_entity method must use IF manual_edit conditional for description/embedding."""
        import inspect
        from services.memory.surrealdb import client as client_mod
        # Read source to confirm the SQL template contains the guard
        src = inspect.getsource(client_mod.SurrealClient.upsert_entity)
        assert "manual_edit" in src, "upsert_entity must contain manual_edit guard in SQL"
        assert "IF manual_edit" in src, "upsert_entity must use IF manual_edit conditional"


# ── MR knowledge ingestion tests ─────────────────────────────────────────────

class TestMRIngestion:
    """Tests for Pass-1 MR knowledge ingestion."""

    def _mr_payload(self, **overrides) -> dict:
        base = {
            "source": "mr",
            "project": "exohub/pinard",
            "repo": "exohub/pinard",
            "iid": 355,
            "scope": "pinard",
            "title": "fix(memory): recall returns no hits for irrelevant queries",
            "description": "Introduced a distance gate in recall. Chosen over a reranker because it is simpler and latency matters.",
            "issues": [{"iid": 193, "title": "recall noise", "description": "Recall returns noisy hits."}],
            "files_changed": ["services/memory/recall_service.py"],
            "merged_at": "2026-08-28T12:00:00Z",
            "author": "pinard-bot",
            "url": "https://gitlab.example.com/example/pinard/-/merge_requests/355",
        }
        base.update(overrides)
        return base

    def test_mr_trivial_yields_zero_entities(self, registry: OntologyRegistry) -> None:
        """LLM returns empty entities → no entity written, processed marker set."""
        payload = self._mr_payload()
        mock_llm = MagicMock()
        mock_llm.complete.return_value = '{"entities": []}'
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.fetch_entity_by_role_name.return_value = None
        # Cursor: not yet processed.
        mock_surreal.get_ingest_cursor.return_value = 0

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal), \
             patch("services.memory.ingester.TokenManager") as mock_tm_cls:
            mock_tm = MagicMock()
            mock_tm.get_client.return_value = mock_llm
            mock_tm_cls.return_value = mock_tm

            from services.memory.ingester import _handle_mr_sync, SurrealCursorStore
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "ok"
        mock_surreal.upsert_entity.assert_not_called()
        # Cursor must be advanced to mark it processed.
        mock_surreal.set_ingest_cursor.assert_called()

    def test_mr_real_extracts_decision(self, registry: OntologyRegistry) -> None:
        """Real MR → LLM returns a decision → entity with provenance=mr."""
        payload = self._mr_payload()
        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "entities": [{
                "role": "decision",
                "name": "distance gate over reranker for recall",
                "description": "Chose distance gate over reranker because it is simpler and latency matters.",
            }]
        })
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.fetch_entity_by_role_name.return_value = None
        mock_surreal.get_ingest_cursor.return_value = 0

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal), \
             patch("services.memory.ingester.embed", return_value=[0.1] * 768):
            from services.memory.ingester import _handle_mr_sync
            mock_tm = MagicMock()
            mock_tm.get_client.return_value = mock_llm
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "ok"
        mock_surreal.upsert_entity.assert_called_once()
        call_kwargs = mock_surreal.upsert_entity.call_args
        assert call_kwargs.kwargs.get("provenance") == "mr" or call_kwargs[1].get("provenance") == "mr" or \
               (call_kwargs[0] and "mr" in str(call_kwargs))
        # Check via keyword args
        kw = mock_surreal.upsert_entity.call_args.kwargs
        assert kw.get("provenance") == "mr"
        assert kw.get("role") == "decision"

    def test_mr_rescope_delta_captured(self, registry: OntologyRegistry) -> None:
        """LLM returns a re-scope delta → emitted as decision entity."""
        payload = self._mr_payload(
            description="The issue wanted a reranker, but we used a distance gate instead because it is simpler.",
        )
        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "entities": [{
                "role": "decision",
                "name": "recall implementation diverged from issue intent",
                "description": "The issue assumed a reranker; the MR used a distance gate because it is simpler and lower latency.",
            }]
        })
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.fetch_entity_by_role_name.return_value = None
        mock_surreal.get_ingest_cursor.return_value = 0

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal), \
             patch("services.memory.ingester.embed", return_value=[0.1] * 768):
            from services.memory.ingester import _handle_mr_sync
            mock_tm = MagicMock()
            mock_tm.get_client.return_value = mock_llm
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "ok"
        mock_surreal.upsert_entity.assert_called_once()
        kw = mock_surreal.upsert_entity.call_args.kwargs
        assert kw.get("role") == "decision"

    def test_mr_reingest_idempotent(self, registry: OntologyRegistry) -> None:
        """Re-delivery of same MR is a no-op (cursor already set)."""
        payload = self._mr_payload()
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        # Simulate already processed: cursor > 0.
        mock_surreal.get_ingest_cursor.return_value = 1

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal):
            from services.memory.ingester import _handle_mr_sync
            mock_tm = MagicMock()
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "ok"
        mock_surreal.upsert_entity.assert_not_called()
        mock_tm.get_client.assert_not_called()

    def test_mr_llm_unavailable_returns_llm_unavailable(self, registry: OntologyRegistry) -> None:
        """LLM unavailable → returns 'llm_unavailable' so caller can nak+delay."""
        from services.memory.token_manager import LLMUnavailable
        payload = self._mr_payload()
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.get_ingest_cursor.return_value = 0

        mock_tm = MagicMock()
        mock_tm.get_client.side_effect = LLMUnavailable("token expired")

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal):
            from services.memory.ingester import _handle_mr_sync
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "llm_unavailable"
        mock_surreal.upsert_entity.assert_not_called()

    def test_mr_supersedes_edge_on_replacement(self, registry: OntologyRegistry) -> None:
        """When an entity with same (role, name) existed before, a supersedes edge is created."""
        payload = self._mr_payload()
        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "entities": [{
                "role": "decision",
                "name": "distance gate over reranker for recall",
                "description": "Chose distance gate over reranker.",
            }]
        })
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        # Simulate a pre-existing entity from a prior MR.
        mock_surreal.fetch_entity_by_role_name.return_value = {
            "id": "entity:oldid",
            "role": "decision",
            "name": "distance gate over reranker for recall",
            "provenance": "mr",
        }
        mock_surreal.get_ingest_cursor.return_value = 0

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal), \
             patch("services.memory.ingester.embed", return_value=[0.1] * 768):
            from services.memory.ingester import _handle_mr_sync
            mock_tm = MagicMock()
            mock_tm.get_client.return_value = mock_llm
            result = _handle_mr_sync(payload, registry, mock_tm)

        assert result == "ok"
        mock_surreal.upsert_entity.assert_called_once()
        # supersedes edge attempted via relate()
        mock_surreal.relate.assert_called()
        relate_kwargs = mock_surreal.relate.call_args.kwargs
        assert relate_kwargs.get("relation") == "supersedes"

    def test_mr_manual_edit_guard_preserved(self) -> None:
        """upsert_entity SQL must preserve manual_edit=true rows."""
        import inspect
        from services.memory.surrealdb import client as client_mod
        src = inspect.getsource(client_mod.SurrealClient.upsert_entity)
        assert "manual_edit" in src
        assert "IF manual_edit" in src

    def test_mr_discussion_not_ingested_v1(self) -> None:
        """No discussion/review-notes route exists in the MR handler (v1)."""
        import inspect
        from services.memory import ingester as ingester_mod
        src = inspect.getsource(ingester_mod._handle_mr_sync)
        assert "discussion" not in src.lower()
        assert "review_note" not in src.lower()

    def test_mr_files_not_embedded(self, registry: OntologyRegistry) -> None:
        """files_changed is stored in data, not in the embedded description."""
        payload = self._mr_payload()
        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "entities": [{
                "role": "decision",
                "name": "use distance gate",
                "description": "Chose distance gate.",
            }]
        })
        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.fetch_entity_by_role_name.return_value = None
        mock_surreal.get_ingest_cursor.return_value = 0

        embedded_texts = []
        def capture_embed(text):
            embedded_texts.append(text)
            return [0.1] * 768

        with patch("services.memory.ingester.SurrealClient", return_value=mock_surreal), \
             patch("services.memory.ingester.embed", side_effect=capture_embed):
            from services.memory.ingester import _handle_mr_sync
            mock_tm = MagicMock()
            mock_tm.get_client.return_value = mock_llm
            _handle_mr_sync(payload, registry, mock_tm)

        # files_changed must not appear in any embedded text
        for text in embedded_texts:
            assert "recall_service.py" not in text
