"""Live smoke tests for services/memory/rollup.py — curate-on-promote invariant.

Requires a running SurrealDB instance (SURREAL_URL, SURREAL_PASS env vars).
Skipped automatically when SURREAL_PASS is not set or SurrealDB is unreachable.

Run manually (port-forward pinard-uat SurrealDB first):
    kubectl port-forward -n pinard-uat svc/pinard-surrealdb 8000:8000
    SURREAL_URL=http://localhost:8000 SURREAL_PASS=<root-pass> \\
        pytest services/memory/tests/test_rollup_live.py -v -m live

Acceptance criteria validated here (issue #161):
- _cleanup_entity_rows actually DELETEs entity rows from a real higher-tier scope.
- _curate_promoted_wiki actually writes a wiki_doc to a real scope (round-trip read,
  status auto_serve).
- A full ScopeRollupEngine.run() over two throwaway group DBs that share an
  overlapping wiki_doc produces a consolidated page at the vignoble scope.
- Higher-tier scopes never contain entity rows after a run.

Throwaway scopes (prefixed "smoke-rollup-") are REMOVE'd after each test.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------

_SURREAL_PASS = os.environ.get("SURREAL_PASS", "")
_SURREAL_URL = os.environ.get("SURREAL_URL", "http://localhost:8000")

pytestmark = pytest.mark.live

_skip_reason = "SURREAL_PASS not set — skipping live SurrealDB rollup tests"
_surreal_available = bool(_SURREAL_PASS)

if _surreal_available:
    try:
        import httpx as _httpx
        _r = _httpx.get(f"{_SURREAL_URL}/health", timeout=2.0)
        _surreal_available = _r.status_code == 200
        if not _surreal_available:
            _skip_reason = f"SurrealDB health check failed (HTTP {_r.status_code})"
    except Exception as _e:
        _surreal_available = False
        _skip_reason = f"SurrealDB not reachable at {_SURREAL_URL}: {_e}"

skip_if_no_surreal = pytest.mark.skipif(
    not _surreal_available, reason=_skip_reason
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SMOKE_VIGNE_A = "smoke-rollup-vigne-a"
_SMOKE_VIGNE_B = "smoke-rollup-vigne-b"
_SMOKE_VIGNOBLE = "smoke-rollup-vignoble"
_SMOKE_VIGNOBLE_DB = f"vignoble-{_SMOKE_VIGNOBLE}"
_SMOKE_GLOBAL_DB = "smoke-rollup-global"

_ALL_SMOKE_DBS = [
    _SMOKE_VIGNE_A,
    _SMOKE_VIGNE_B,
    _SMOKE_VIGNOBLE_DB,
    _SMOKE_GLOBAL_DB,
]


def _make_client(group_id: str):
    from services.memory.surrealdb.client import SurrealClient, _schema_applied
    _schema_applied.discard(group_id)
    return SurrealClient(
        group_id=group_id,
        url=_SURREAL_URL,
        user=os.environ.get("SURREAL_USER", "root"),
        password=_SURREAL_PASS,
    )


def _drop_db(group_id: str) -> None:
    """Remove a throwaway smoke DB from SurrealDB."""
    try:
        c = _make_client(group_id)
        c.query(f"REMOVE DATABASE IF EXISTS `{group_id}`")
        c.close()
    except Exception:
        pass


def _cleanup_all() -> None:
    for db in _ALL_SMOKE_DBS:
        _drop_db(db)


def _noop_embed(text: str) -> list[float]:
    """Return a non-zero embedding so cosine similarity works."""
    # Use a deterministic non-zero vector so identical text → identical embedding
    # and different text → slightly different but still high-cosine embedding.
    base = [1.0] * 64 + [0.0] * (1024 - 64)
    return base


def _stub_llm(response: str = "# Summary\n\nConsolidated stub content.\n") -> MagicMock:
    llm = MagicMock()
    llm.complete.return_value = response
    return llm


def _seed_entity(surreal, role: str, name: str, description: str = "") -> None:
    surreal.upsert_entity(
        role=role,
        name=name,
        description=description,
        embedding=_noop_embed(f"{name} {description}"),
    )


def _seed_wiki_doc(surreal, title: str, body: str, path: str) -> None:
    surreal.upsert_wiki_doc(
        title=title,
        body=body,
        frontmatter={"type": "concept", "source": "smoke"},
        path=path,
        confidence=0.8,
        embedding=_noop_embed(f"{title}\n{body}"),
    )


def _count_entity_rows(group_id: str) -> int:
    try:
        c = _make_client(group_id)
        rows = c.query("SELECT count() FROM entity GROUP ALL")
        c.close()
        if rows and rows[0]:
            r = rows[0]
            r = r[0] if isinstance(r, list) else r
            return int(r.get("count", 0)) if isinstance(r, dict) else 0
    except Exception:
        pass
    return 0


def _count_wiki_docs(group_id: str) -> int:
    try:
        c = _make_client(group_id)
        rows = c.query("SELECT count() FROM wiki_doc GROUP ALL")
        c.close()
        if rows and rows[0]:
            r = rows[0]
            r = r[0] if isinstance(r, list) else r
            return int(r.get("count", 0)) if isinstance(r, dict) else 0
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_if_no_surreal
class TestRollupLive:
    """Live smoke tests — require a running SurrealDB."""

    def setup_method(self):
        _cleanup_all()

    def teardown_method(self):
        _cleanup_all()

    def test_cleanup_entity_rows_deletes_from_real_scope(self):
        """_cleanup_entity_rows actually DELETEs entity rows from a real higher-tier scope."""
        from services.memory.rollup import _cleanup_entity_rows

        # Seed an entity directly into the vignoble scope (simulating a leaked row).
        surreal = _make_client(_SMOKE_VIGNOBLE_DB)
        _seed_entity(surreal, role="decision", name="leaked entity", description="should be deleted")
        surreal.close()

        # Verify it's there.
        assert _count_entity_rows(_SMOKE_VIGNOBLE_DB) == 1

        # Run cleanup.
        _cleanup_entity_rows(_SMOKE_VIGNOBLE_DB)

        # Verify it's gone.
        assert _count_entity_rows(_SMOKE_VIGNOBLE_DB) == 0

    def test_curate_promoted_wiki_writes_wiki_doc_round_trip(self):
        """_curate_promoted_wiki writes a wiki_doc to a real scope; read it back, status auto_serve."""
        from services.memory.rollup import _curate_promoted_wiki

        candidates = [
            {
                "title": "Smoke Test Consolidated Concept",
                "sources": [
                    {
                        "kind": "wiki",
                        "group_id": _SMOKE_VIGNE_A,
                        "title": "Concept from vigne A",
                        "body": "Details from vigne A.",
                        "path": "decisions/concept-a",
                    },
                    {
                        "kind": "wiki",
                        "group_id": _SMOKE_VIGNE_B,
                        "title": "Concept from vigne B",
                        "body": "Details from vigne B.",
                        "path": "decisions/concept-b",
                    },
                ],
            }
        ]

        n = _curate_promoted_wiki(
            _SMOKE_VIGNOBLE_DB,
            candidates,
            llm_client=_stub_llm(),
            embed_fn=_noop_embed,
        )
        assert n == 1, f"Expected 1 wiki_doc written, got {n}"

        # Round-trip: read it back from SurrealDB.
        surreal = _make_client(_SMOKE_VIGNOBLE_DB)
        rows = surreal.query(
            "SELECT title, status, confidence FROM wiki_doc "
            "WHERE title = 'Smoke Test Consolidated Concept' LIMIT 1"
        )
        surreal.close()

        assert rows and rows[0], "wiki_doc not found after _curate_promoted_wiki"
        row = rows[0]
        row = row[0] if isinstance(row, list) else row
        assert isinstance(row, dict), f"Unexpected row type: {type(row)}"
        assert row.get("title") == "Smoke Test Consolidated Concept"
        assert row.get("status") == "auto_serve", (
            f"Expected status=auto_serve (confidence=0.75 ≥ 0.7), got {row.get('status')}"
        )
        assert float(row.get("confidence", 0)) >= 0.7

    def test_higher_tier_scope_has_no_entity_rows_after_run(self):
        """After a rollup run, vignoble and global scopes contain NO entity rows."""
        from services.memory.rollup import ScopeRollupEngine, _vignoble_db, GLOBAL_DB
        from unittest.mock import patch

        # Pre-seed entity rows in the vignoble scope to simulate leaked rows.
        surreal = _make_client(_SMOKE_VIGNOBLE_DB)
        _seed_entity(surreal, role="artifact", name="leaked artifact", description="")
        _seed_entity(surreal, role="decision", name="leaked decision", description="")
        surreal.close()
        assert _count_entity_rows(_SMOKE_VIGNOBLE_DB) == 2

        membership = {_SMOKE_VIGNOBLE: [_SMOKE_VIGNE_A, _SMOKE_VIGNE_B]}

        with patch(
            "services.memory.rollup._load_all_vignoble_memberships",
            return_value=membership,
        ), patch(
            "services.memory.rollup.GLOBAL_DB", _SMOKE_GLOBAL_DB
        ):
            engine = ScopeRollupEngine(
                vignobles_base_dir="/fake/vignobles",
                embed_fn=_noop_embed,
                llm_client=_stub_llm(),
            )
            engine.run()

        # Vignoble scope must have 0 entity rows.
        assert _count_entity_rows(_SMOKE_VIGNOBLE_DB) == 0, (
            "Entity rows leaked into vignoble scope were not cleaned up"
        )
        # Global scope must have 0 entity rows.
        assert _count_entity_rows(_SMOKE_GLOBAL_DB) == 0, (
            "Entity rows leaked into global scope were not cleaned up"
        )

    def test_full_run_with_overlapping_wiki_docs_produces_consolidated_page(self):
        """Full run: two vignes with semantically similar wiki_docs → consolidated wiki_doc at vignoble scope."""
        from services.memory.rollup import ScopeRollupEngine
        from unittest.mock import patch

        # Seed similar wiki_docs into both vignes (same embedding → same cluster).
        surreal_a = _make_client(_SMOKE_VIGNE_A)
        _seed_wiki_doc(
            surreal_a,
            title="Use Conventional Commits",
            body="Always prefix commits with fix: or feat: for automated changelog generation.",
            path="decisions/use-conventional-commits",
        )
        surreal_a.close()

        surreal_b = _make_client(_SMOKE_VIGNE_B)
        _seed_wiki_doc(
            surreal_b,
            title="Conventional Commit Policy",
            body="Commit messages must start with fix: or feat: to drive CI/CD pipelines.",
            path="decisions/conventional-commit-policy",
        )
        surreal_b.close()

        # Vignoble scope starts empty.
        assert _count_wiki_docs(_SMOKE_VIGNOBLE_DB) == 0

        membership = {_SMOKE_VIGNOBLE: [_SMOKE_VIGNE_A, _SMOKE_VIGNE_B]}

        with patch(
            "services.memory.rollup._load_all_vignoble_memberships",
            return_value=membership,
        ), patch(
            "services.memory.rollup.GLOBAL_DB", _SMOKE_GLOBAL_DB
        ):
            engine = ScopeRollupEngine(
                vignobles_base_dir="/fake/vignobles",
                embed_fn=_noop_embed,
                llm_client=_stub_llm(
                    "# Summary\n\nBoth projects require conventional commits.\n"
                ),
            )
            counts = engine.run()

        # Must have synthesised at least one consolidated wiki_doc.
        assert counts["vignoble_wiki_synthesized"] >= 1, (
            f"Expected vignoble_wiki_synthesized >= 1, got {counts}"
        )
        # Vignoble scope must have wiki_docs but NO entity rows.
        assert _count_wiki_docs(_SMOKE_VIGNOBLE_DB) >= 1, (
            "No wiki_doc written to vignoble scope after rollup"
        )
        assert _count_entity_rows(_SMOKE_VIGNOBLE_DB) == 0, (
            "Entity rows found in vignoble scope — invariant violated"
        )
        # Promoted value must be 0 (no entity copy-up ever).
        assert counts["vignoble_promoted"] == 0
        assert counts["global_promoted"] == 0
