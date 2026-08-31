"""Tests for the Rosetta embedding client.

Unit tests mock the HTTP layer; the round-trip test (marked with
pytest.mark.integration) requires a live Rosetta endpoint and a live
SurrealDB instance.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest
import httpx
from unittest.mock import patch, MagicMock

from services.memory.embeddings import embed, embed_batch, EmbeddingError, EMBEDDING_DIM


def _mock_response(vectors: list[list[float]], status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = ""
    resp.json.return_value = {
        "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    }
    return resp


def _fake_vec(dim: int = EMBEDDING_DIM) -> list[float]:
    return [0.1] * dim


class TestEmbedBatch:
    def test_empty_input_returns_empty(self):
        result = embed_batch([])
        assert result == []

    def test_single_text(self):
        vec = _fake_vec()
        with patch("httpx.post", return_value=_mock_response([vec])):
            result = embed_batch(["hello world"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM

    def test_multiple_texts(self):
        vecs = [_fake_vec(), _fake_vec()]
        with patch("httpx.post", return_value=_mock_response(vecs)):
            result = embed_batch(["text one", "text two"])
        assert len(result) == 2

    def test_out_of_order_results_sorted(self):
        # API returns index 1 before index 0 — must be sorted.
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = ""
        resp.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.2] * EMBEDDING_DIM},
                {"index": 0, "embedding": [0.1] * EMBEDDING_DIM},
            ]
        }
        with patch("httpx.post", return_value=resp):
            result = embed_batch(["a", "b"])
        assert result[0][0] == pytest.approx(0.1)
        assert result[1][0] == pytest.approx(0.2)

    def test_http_error_raises_embedding_error(self):
        resp = _mock_response([], status=503)
        resp.text = "Service Unavailable"
        with patch("httpx.post", return_value=resp):
            with pytest.raises(EmbeddingError, match="HTTP 503"):
                embed_batch(["text"])

    def test_wrong_dimension_raises_embedding_error(self):
        bad_vec = [0.1] * 512  # wrong dim
        with patch("httpx.post", return_value=_mock_response([bad_vec])):
            with pytest.raises(EmbeddingError, match="1024-dim"):
                embed_batch(["text"])

    def test_request_error_raises_embedding_error(self):
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(EmbeddingError, match="request failed"):
                embed_batch(["text"])

    def test_malformed_response_raises_embedding_error(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = ""
        resp.json.return_value = {"unexpected": "shape"}
        with patch("httpx.post", return_value=resp):
            with pytest.raises(EmbeddingError, match="Unexpected"):
                embed_batch(["text"])


class TestEmbed:
    def test_embed_single_returns_vector(self):
        vec = _fake_vec()
        with patch("httpx.post", return_value=_mock_response([vec])):
            result = embed("hello")
        assert len(result) == EMBEDDING_DIM

    def test_embed_passes_model_and_url(self):
        vec = _fake_vec()
        with patch("httpx.post", return_value=_mock_response([vec])) as mock_post:
            embed("hello", model="custom-model", url="http://custom:8080")
        call_kwargs = mock_post.call_args
        assert "custom-model" in str(call_kwargs)
        assert "custom:8080" in str(call_kwargs)


# ─── Integration test (requires live Rosetta + SurrealDB) ────────────────────


@pytest.mark.integration
class TestRosettaSurrealRoundTrip:
    """Verifies that an embedding can be written to SurrealDB and recalled via HNSW.

    Run with: pytest -m integration services/memory/tests/test_embeddings.py
    Requires: ROSETTA_URL, SURREAL_URL, SURREAL_PASS env vars.
    """

    def test_embed_store_recall(self):
        from services.memory.embeddings import embed
        from services.memory.surrealdb.client import SurrealClient

        # Embed two texts.
        text_a = "use fix: prefix for all commits"
        text_b = "always add unit tests for new functions"
        vec_a = embed(text_a)
        vec_b = embed(text_b)
        assert len(vec_a) == EMBEDDING_DIM
        assert len(vec_b) == EMBEDDING_DIM

        # Write to SurrealDB.
        with SurrealClient(group_id="test-embed-roundtrip") as db:
            db.upsert_entity(
                role="action",
                name="commit-prefix-rule",
                description=text_a,
                embedding=vec_a,
            )
            db.upsert_entity(
                role="action",
                name="unit-test-rule",
                description=text_b,
                embedding=vec_b,
            )

            # Recall by vector similarity — query with vec_a, expect commit-prefix-rule first.
            results = db.recall(embedding=vec_a, limit=5)
            assert results, "HNSW recall returned no results"
            top = results[0]
            assert top["name"] == "commit-prefix-rule", (
                f"Expected 'commit-prefix-rule' as top result, got '{top['name']}'"
            )
