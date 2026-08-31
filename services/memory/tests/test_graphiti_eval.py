"""Tests for the Graphiti eval sidecar.

Unit tests mock the Graphiti SDK; the integration test (marked with
pytest.mark.integration) requires a live FalkorDB + Anthropic API key.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGraphitiEvalClient:
    """Tests for the graphiti eval client (now backed by LLMClient)."""

    def test_build_llm_client_uses_static_key(self, monkeypatch):
        """build_llm_client() picks up ANTHROPIC_API_KEY as a static-key provider."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        from services.memory.llm_client import build_llm_client, _StaticKeyProvider

        llm = build_llm_client()
        assert isinstance(llm._token_provider, _StaticKeyProvider)
        assert llm._token_provider.get_token() == "test-key-123"

    def test_build_llm_client_raises_when_no_key(self, monkeypatch):
        """LLMAuthError is raised when no key source is configured."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MEMORY_TOKEN_URL", raising=False)
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        from services.memory.llm_client import build_llm_client, LLMAuthError

        llm = build_llm_client()
        with pytest.raises(LLMAuthError, match="No static API key"):
            llm._token_provider.get_token()

    def test_build_llm_client_uses_token_url(self, monkeypatch):
        """build_llm_client() auto-detects URL auth from MEMORY_TOKEN_URL."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("MEMORY_TOKEN_URL", "http://token-server/key")
        monkeypatch.delenv("MEMORY_LLM_AUTH", raising=False)
        monkeypatch.delenv("MEMORY_LLM_API", raising=False)
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"api_key": "fetched-key"}
        mock_resp.raise_for_status = MagicMock()

        from services.memory.llm_client import build_llm_client
        with patch("httpx.get", return_value=mock_resp):
            llm = build_llm_client()
            result = llm._token_provider.get_token()

        assert result == "fetched-key"


class TestGraphitiEvalVerify:
    @pytest.mark.asyncio
    async def test_run_verify_success(self):
        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()
        mock_graphiti.search = AsyncMock(
            return_value=[{"fact": "SurrealDB is the store of record"}]
        )

        mock_add_episode = AsyncMock()

        with (
            patch(
                "services.memory.graphiti_eval.verify.build_graphiti",
                return_value=mock_graphiti,
            ),
            patch(
                "services.memory.graphiti_eval.verify.add_episode",
                mock_add_episode,
            ),
            patch(
                "services.memory.graphiti_eval.verify.search",
                return_value=[{"fact": "result"}],
            ),
        ):
            from services.memory.graphiti_eval.verify import run_verify
            result = await run_verify()

        assert result is True
        mock_graphiti.build_indices_and_constraints.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_verify_empty_results_fails(self):
        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()

        with (
            patch(
                "services.memory.graphiti_eval.verify.build_graphiti",
                return_value=mock_graphiti,
            ),
            patch("services.memory.graphiti_eval.verify.add_episode", AsyncMock()),
            patch(
                "services.memory.graphiti_eval.verify.search",
                return_value=[],
            ),
        ):
            from services.memory.graphiti_eval.verify import run_verify
            result = await run_verify()

        assert result is False

    @pytest.mark.asyncio
    async def test_run_verify_init_failure(self):
        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock(
            side_effect=ConnectionError("FalkorDB unreachable")
        )

        with patch(
            "services.memory.graphiti_eval.verify.build_graphiti",
            return_value=mock_graphiti,
        ):
            from services.memory.graphiti_eval.verify import run_verify
            result = await run_verify()

        assert result is False


# ─── Export tests ───────────────────────────────────────────────────────────────


class TestSurrealExport:
    def _make_client(self, entity_rows=None, edge_rows=None):
        client = MagicMock()

        def query_side_effect(sql, *args, **kwargs):
            sql_lower = sql.lower()
            if "from entity" in sql_lower:
                return [{'result': entity_rows or []}]
            # Edge relation queries — return edge_rows for the first relation, empty for rest.
            return [{'result': edge_rows or []}]

        client.query.side_effect = query_side_effect
        return client

    def test_export_nodes_writes_ndjson(self, tmp_path):
        from services.memory.graphiti_eval.export import export_nodes

        rows = [
            {'id': 'entity:abc', 'role': 'task', 'name': 'Build', 'description': 'build step', 'data': {}},
            {'id': 'entity:xyz', 'role': 'artifact', 'name': 'output.tar', 'description': '', 'data': {'size': 1024}},
        ]
        client = self._make_client(entity_rows=rows)
        out = tmp_path / 'nodes.jsonl'
        count = export_nodes(client, out)

        assert count == 2
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first['id'] == 'entity:abc'
        assert first['role'] == 'task'
        assert first['name'] == 'Build'

    def test_export_nodes_empty_db(self, tmp_path):
        from services.memory.graphiti_eval.export import export_nodes

        client = self._make_client(entity_rows=[])
        out = tmp_path / 'nodes.jsonl'
        count = export_nodes(client, out)

        assert count == 0
        assert out.read_text() == ''

    def test_export_edges_skips_missing_tables(self, tmp_path):
        from services.memory.graphiti_eval.export import export_edges

        client = MagicMock()
        client.query.side_effect = Exception('table not found')
        out = tmp_path / 'edges.jsonl'
        count = export_edges(client, out, relations=['depends_on'])

        assert count == 0

    def test_export_edges_writes_ndjson(self, tmp_path):
        from services.memory.graphiti_eval.export import export_edges

        edge_rows = [
            {'in': 'entity:a', 'out': 'entity:b', 'confidence': 0.9,
             'description': 'A depends on B', 'data': {}},
        ]
        client = MagicMock()
        client.query.return_value = [{'result': edge_rows}]
        out = tmp_path / 'edges.jsonl'
        count = export_edges(client, out, relations=['depends_on'])

        assert count == 1
        rec = json.loads(out.read_text().strip())
        assert rec['from_id'] == 'entity:a'
        assert rec['to_id'] == 'entity:b'
        assert rec['relation'] == 'depends_on'
        assert rec['confidence'] == 0.9

    def test_extract_record_id_dict_format(self):
        from services.memory.graphiti_eval.export import _extract_record_id

        raw = {'tb': 'entity', 'id': {'String': 'ulid123'}}
        assert _extract_record_id(raw) == 'entity:ulid123'

    def test_extract_record_id_string(self):
        from services.memory.graphiti_eval.export import _extract_record_id

        assert _extract_record_id('entity:abc') == 'entity:abc'

    def test_export_jsonl_returns_stats(self, tmp_path):
        from services.memory.graphiti_eval.export import export_jsonl

        entity_rows = [
            {'id': 'entity:n1', 'role': 'task', 'name': 'T', 'description': '', 'data': {}},
        ]
        client = MagicMock()
        # First call (nodes query), subsequent calls (edge queries).
        client.query.side_effect = [
            [{'result': entity_rows}],  # nodes query
            [{'result': []}],           # edges query (no edges)
        ]
        stats = export_jsonl(
            client,
            nodes_path=tmp_path / 'nodes.jsonl',
            edges_path=tmp_path / 'edges.jsonl',
            relations=[],
        )
        assert stats == {'nodes': 1, 'edges': 0}


# ─── Ingest tests ─────────────────────────────────────────────────────────────


class TestGraphitiIngest:
    def _write_jsonl(self, tmp_path, filename, records):
        p = tmp_path / filename
        lines = "\n".join(json.dumps(r) for r in records) + "\n"
        p.write_text(lines)
        return p

    @pytest.mark.asyncio
    async def test_ingest_creates_episodes(self, tmp_path):
        from services.memory.graphiti_eval.ingest import ingest_jsonl

        nodes = [
            {'id': 'entity:a', 'role': 'task', 'name': 'Build', 'description': 'compile step', 'data': {}},
            {'id': 'entity:b', 'role': 'artifact', 'name': 'out.tar', 'description': '', 'data': {}},
        ]
        edges = [
            {'from_id': 'entity:a', 'to_id': 'entity:b', 'relation': 'produces',
             'confidence': 1.0, 'description': 'produces output', 'data': {}},
        ]
        nodes_path = self._write_jsonl(tmp_path, 'nodes.jsonl', nodes)
        edges_path = self._write_jsonl(tmp_path, 'edges.jsonl', edges)

        mock_graphiti = MagicMock()
        mock_add = AsyncMock()

        with patch('services.memory.graphiti_eval.ingest.add_episode', mock_add):
            stats = await ingest_jsonl(mock_graphiti, 'test-group',
                                       nodes_path=nodes_path, edges_path=edges_path)

        assert stats['episodes'] == 2
        assert stats['skipped'] == 0
        assert mock_add.call_count == 2

    @pytest.mark.asyncio
    async def test_ingest_skips_failed_episodes(self, tmp_path):
        from services.memory.graphiti_eval.ingest import ingest_jsonl

        nodes = [{'id': 'entity:bad', 'role': 'task', 'name': 'Fail', 'description': '', 'data': {}}]
        nodes_path = self._write_jsonl(tmp_path, 'nodes.jsonl', nodes)
        edges_path = self._write_jsonl(tmp_path, 'edges.jsonl', [])

        mock_graphiti = MagicMock()
        mock_add = AsyncMock(side_effect=RuntimeError('Graphiti unavailable'))

        with patch('services.memory.graphiti_eval.ingest.add_episode', mock_add):
            stats = await ingest_jsonl(mock_graphiti, 'test-group',
                                       nodes_path=nodes_path, edges_path=edges_path)

        assert stats['episodes'] == 0
        assert stats['skipped'] == 1

    @pytest.mark.asyncio
    async def test_ingest_empty_files(self, tmp_path):
        from services.memory.graphiti_eval.ingest import ingest_jsonl

        nodes_path = tmp_path / 'nodes.jsonl'
        edges_path = tmp_path / 'edges.jsonl'
        nodes_path.write_text('')
        edges_path.write_text('')

        mock_graphiti = MagicMock()
        mock_add = AsyncMock()

        with patch('services.memory.graphiti_eval.ingest.add_episode', mock_add):
            stats = await ingest_jsonl(mock_graphiti, 'test-group',
                                       nodes_path=nodes_path, edges_path=edges_path)

        assert stats == {'episodes': 0, 'skipped': 0}
        mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_episode_content_includes_edges(self, tmp_path):
        from services.memory.graphiti_eval.ingest import ingest_jsonl, _build_episode_content

        node = {'id': 'entity:a', 'role': 'task', 'name': 'Build',
                'description': 'compile step', 'data': {'lang': 'python'}}
        edges = [
            {'from_id': 'entity:a', 'to_id': 'entity:b', 'relation': 'produces',
             'confidence': 0.8, 'description': 'produces output', 'data': {}},
        ]
        content = _build_episode_content(node, edges)
        assert '[task] Build' in content
        assert 'compile step' in content
        assert 'produces' in content
        assert 'entity:b' in content
        assert 'produces output' in content

    def test_load_jsonl_missing_file(self, tmp_path):
        from services.memory.graphiti_eval.ingest import _load_jsonl

        result = _load_jsonl(tmp_path / 'nonexistent.jsonl')
        assert result == []


# ─── Assessment tests ─────────────────────────────────────────────────────────


class TestGraphitiAssessment:
    @pytest.mark.asyncio
    async def test_run_assessment_returns_probe_results(self):
        from services.memory.graphiti_eval.assess import run_assessment, PROBES

        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()

        mock_result = [
            {'fact': 'SurrealDB is the store of record',
             'source_node': {'name': 'SurrealDB'},
             'target_node': {'name': 'memory layer'}},
        ]

        with (
            patch('services.memory.graphiti_eval.assess.build_graphiti',
                  return_value=mock_graphiti),
            patch('services.memory.graphiti_eval.assess.graphiti_search',
                  AsyncMock(return_value=mock_result)),
        ):
            results = await run_assessment('test-group', probes=PROBES[:2], limit=3)

        # Two probes + one summary object.
        assert len(results) == 3
        summary = results[-1]
        assert summary['probe'] == '__summary__'
        assert summary['total_probes'] == 2

    @pytest.mark.asyncio
    async def test_run_assessment_handles_probe_failure(self):
        from services.memory.graphiti_eval.assess import run_assessment

        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()
        probes = [{'probe': 'bad_probe', 'query': 'trigger error'}]

        with (
            patch('services.memory.graphiti_eval.assess.build_graphiti',
                  return_value=mock_graphiti),
            patch('services.memory.graphiti_eval.assess.graphiti_search',
                  AsyncMock(side_effect=RuntimeError('search failed'))),
        ):
            results = await run_assessment('test-group', probes=probes)

        probe_result = results[0]
        assert probe_result['probe'] == 'bad_probe'
        assert 'error' in probe_result
        assert probe_result['result_count'] == 0

    @pytest.mark.asyncio
    async def test_run_assessment_dedup_tracking(self):
        from services.memory.graphiti_eval.assess import run_assessment

        mock_graphiti = MagicMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()
        # Same fact returned for both probes — should count as duplicate.
        same_fact = [{'fact': 'repeated fact'}]
        probes = [
            {'probe': 'p1', 'query': 'query one'},
            {'probe': 'p2', 'query': 'query two'},
        ]

        with (
            patch('services.memory.graphiti_eval.assess.build_graphiti',
                  return_value=mock_graphiti),
            patch('services.memory.graphiti_eval.assess.graphiti_search',
                  AsyncMock(return_value=same_fact)),
        ):
            results = await run_assessment('test-group', probes=probes)

        summary = results[-1]
        assert summary['cross_probe_duplicates'] >= 1
        assert summary['unique_facts'] == 1


# ─── Integration test (requires live FalkorDB + Anthropic key) ───────────────


@pytest.mark.integration
@pytest.mark.asyncio
class TestGraphitiLiveRoundTrip:
    """Run with: pytest -m integration services/memory/tests/test_graphiti_eval.py
    Requires: FALKORDB_URL, ANTHROPIC_API_KEY (or MEMORY_TOKEN_URL) env vars.
    """

    async def test_add_episode_and_search(self):
        from services.memory.graphiti_eval.client import build_graphiti, add_episode, search

        group_id = "pinard-test-live"
        graphiti = build_graphiti(group_id)
        await graphiti.build_indices_and_constraints()

        await add_episode(
            graphiti,
            group_id=group_id,
            name="test-episode",
            content="SurrealDB is the store of record for the pinard memory layer.",
            source="pinard-test",
        )

        results = await search(graphiti, "SurrealDB store of record", [group_id], limit=5)
        assert results, "Expected at least one result from Graphiti"
