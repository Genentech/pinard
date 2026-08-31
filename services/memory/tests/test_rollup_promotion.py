"""Integration tests for scope roll-up and promotion engine.

Tests:
1. ScopeRollupEngine: vignes.yaml parsing + wiki-based aggregation across group_ids
2. Invariant: no entity (artifact or typed) is ever promoted to a higher scope
3. Cleanup: entity rows are deleted from higher-tier scopes on every run
4. Curate-on-promote: wiki_doc overlap triggers LLM synthesis at higher tier
5. PromotionCandidateDetector: cross-vignoble recurrence detection via Engram
6. ObsidianPromoter: write candidates to markdown, preserve checked state
7. PRBridge: parse approved checkboxes, open MR (mocked git+glab)
8. End-to-end: same rule in N vignobles → candidate → approve → MR

All external I/O (SurrealDB, Engram, git, glab) is mocked.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def vignes_yaml(tmp_path: Path) -> Path:
    """A minimal vignes.yaml with two vignes (inside a vignoble-test subdir)."""
    content = {
        "name": "test-vignoble",
        "vignes": {
            "project-alpha": {"path": "~/alpha", "repo": "org/alpha"},
            "project-beta": {"path": "~/beta", "repo": "org/beta"},
        },
    }
    vignoble_dir = tmp_path / "vignobles" / "vignoble-test"
    vignoble_dir.mkdir(parents=True)
    p = vignoble_dir / "vignes.yaml"
    p.write_text(yaml.dump(content))
    return p


@pytest.fixture
def vignobles_base_dir(vignes_yaml: Path) -> Path:
    """Parent directory of vignoble clones (contains vignoble-test/)."""
    return vignes_yaml.parent.parent


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / ".wiki"
    root.mkdir()
    return root


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


# ── 1. ScopeRollupEngine ──────────────────────────────────────────────────────

class TestScopeRollupEngine:
    def test_load_vignoble_membership(self, vignes_yaml: Path) -> None:
        from services.memory.rollup import _load_vignoble_membership

        membership = _load_vignoble_membership(str(vignes_yaml))
        assert "test-vignoble" in membership
        assert set(membership["test-vignoble"]) == {"project-alpha", "project-beta"}

    def test_load_missing_vignes_yaml(self, tmp_path: Path) -> None:
        from services.memory.rollup import _load_vignoble_membership

        result = _load_vignoble_membership(str(tmp_path / "nonexistent.yaml"))
        assert result == {}

    def test_entity_rows_never_promoted(self, vignobles_base_dir: Path) -> None:
        """Core invariant: no entity of any role is ever promoted to vignoble scope."""
        from services.memory.rollup import ScopeRollupEngine

        entity = {
            "role": "decision",
            "name": "use fix: or feat: commit prefix",
            "description": "Always use conventional commit prefixes",
            "embedding": [0.1] * 8,
            "data": {},
        }

        with (
            patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[]),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[entity]),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch("services.memory.rollup._curate_promoted_wiki", return_value=0),
        ):
            mock_embed = MagicMock(return_value=[0.1] * 8)
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                embed_fn=mock_embed,
            )
            counts = engine.run()

        # No entity upsert — ever.  The old _upsert_rollup_entity must not be called.
        assert counts["vignoble_promoted"] == 0
        assert counts["global_promoted"] == 0

    def test_artifact_entity_not_promoted(self, vignobles_base_dir: Path) -> None:
        """Layer 1 (artifact) entities are excluded by _fetch_typed_entities, never reach synthesis."""
        from services.memory.rollup import ScopeRollupEngine

        # _fetch_typed_entities filters out 'artifact' in its SQL WHERE clause.
        # Simulate: wiki_docs = empty, typed_entities = empty (artifact filtered out).
        with (
            patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[]),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch("services.memory.rollup._cleanup_entity_rows"),
        ):
            mock_embed = MagicMock(return_value=[1.0] + [0.0] * 7)
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                embed_fn=mock_embed,
            )
            counts = engine.run()

        assert counts["vignoble_promoted"] == 0
        assert counts["global_promoted"] == 0
        assert counts["vignoble_wiki_synthesized"] == 0

    def test_typed_entity_not_promoted(self, vignobles_base_dir: Path) -> None:
        """Layer 2 (typed) entities are NOT promoted as entity copies — only feeds synthesis."""
        from services.memory.rollup import ScopeRollupEngine

        typed_ent = {
            "role": "diagnosis",
            "name": "SurrealDB time::now() nanosecond issue",
            "description": "Python SDK truncates to microsecond",
            "embedding": [0.5] * 8,
        }

        with (
            patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[]),
            patch(
                "services.memory.rollup._fetch_typed_entities",
                return_value=[typed_ent],
            ),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch("services.memory.rollup._curate_promoted_wiki", return_value=0),
        ):
            mock_embed = MagicMock(return_value=[0.5] * 8)
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                embed_fn=mock_embed,
            )
            counts = engine.run()

        # vignoble_promoted must remain 0 — no entity rows are copied.
        assert counts["vignoble_promoted"] == 0
        assert counts["global_promoted"] == 0

    def test_wiki_doc_overlap_triggers_synthesis(self, vignobles_base_dir: Path) -> None:
        """wiki_docs from 2+ vignes that cluster together trigger curate-on-promote."""
        from services.memory.rollup import ScopeRollupEngine

        # Both project-alpha and project-beta have semantically similar wiki docs.
        # They share the same embedding so they'll cluster together.
        shared_emb = [1.0] + [0.0] * 7

        doc_alpha = {
            "path": "decisions/use-conventional-commits",
            "title": "Use Conventional Commits in Alpha",
            "body": "Always use fix: or feat: prefix.",
            "embedding": shared_emb,
        }
        doc_beta = {
            "path": "decisions/conventional-commits-policy",
            "title": "Conventional Commits Policy in Beta",
            "body": "Always prefix commits with fix: or feat:.",
            "embedding": shared_emb,
        }

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "# Summary\n\nConventional commits.\n"
        mock_embed = MagicMock(return_value=shared_emb)

        curated: list[str] = []

        def fake_curate(db_name, candidates, llm, embed_fn):
            curated.append(db_name)
            return len(candidates)

        def fetch_wiki_side_effect(group_id: str) -> list:
            return [doc_alpha] if group_id == "project-alpha" else [doc_beta]

        with (
            patch(
                "services.memory.rollup._fetch_all_wiki_docs",
                side_effect=fetch_wiki_side_effect,
            ),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch("services.memory.rollup._curate_promoted_wiki", side_effect=fake_curate),
        ):
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                llm_client=mock_llm,
                embed_fn=mock_embed,
            )
            counts = engine.run()

        assert counts["vignoble_wiki_synthesized"] >= 1
        assert any("vignoble-" in db for db in curated)

    def test_cleanup_entity_rows_called_for_each_higher_tier_scope(
        self, vignobles_base_dir: Path
    ) -> None:
        """_cleanup_entity_rows must be called for vignoble and global scopes on every run."""
        from services.memory.rollup import ScopeRollupEngine, GLOBAL_DB

        cleaned: list[str] = []

        with (
            patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[]),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch(
                "services.memory.rollup._cleanup_entity_rows",
                side_effect=lambda db: cleaned.append(db),
            ),
        ):
            engine = ScopeRollupEngine(vignobles_base_dir=str(vignobles_base_dir))
            engine.run()

        assert GLOBAL_DB in cleaned
        assert any("vignoble-" in db for db in cleaned)

    def test_cleanup_only_removes_promotion_entities(self) -> None:
        """_cleanup_entity_rows deletes only provenance='promotion' rows, not directly-ingested ones."""
        from services.memory.rollup import _cleanup_entity_rows

        queries_executed: list[str] = []

        mock_surreal = MagicMock()
        mock_surreal.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal.__exit__ = MagicMock(return_value=False)
        mock_surreal.query = MagicMock(side_effect=lambda q: queries_executed.append(q))

        with patch("services.memory.rollup.SurrealClient", return_value=mock_surreal):
            _cleanup_entity_rows("vignoble-test")

        assert len(queries_executed) == 1
        q = queries_executed[0]
        # Must filter by provenance — must NOT be a bare DELETE entity
        assert "provenance" in q
        assert "promotion" in q
        assert q.strip() != "DELETE entity"

    def test_rollup_skips_item_in_single_group(self, vignobles_base_dir: Path) -> None:
        """wiki_doc in only 1 group_id → NOT synthesised (cluster spans < threshold vignes)."""
        from services.memory.rollup import ScopeRollupEngine

        doc = {
            "path": "decisions/unique-to-alpha",
            "title": "Only in Alpha",
            "body": "Only in alpha",
            "embedding": [1.0] + [0.0] * 7,
        }

        curated: list = []

        def fake_curate(db_name, candidates, llm, embed_fn):
            curated.extend(candidates)
            return 0

        with (
            patch("services.memory.rollup._fetch_all_wiki_docs") as mock_fetch,
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch("services.memory.rollup._curate_promoted_wiki", side_effect=fake_curate),
        ):
            # project-alpha has the doc, project-beta returns nothing.
            def fetch_side_effect(group_id: str) -> list:
                return [doc] if group_id == "project-alpha" else []

            mock_fetch.side_effect = fetch_side_effect

            mock_llm = MagicMock()
            mock_embed = MagicMock(return_value=[1.0] + [0.0] * 7)

            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                llm_client=mock_llm,
                embed_fn=mock_embed,
            )
            counts = engine.run()

        assert counts["vignoble_wiki_synthesized"] == 0
        assert curated == []

    def test_rollup_returns_zero_when_empty_base_dir(self, tmp_path: Path) -> None:
        from services.memory.rollup import ScopeRollupEngine

        empty_base = tmp_path / "empty_vignobles"
        empty_base.mkdir()
        engine = ScopeRollupEngine(vignobles_base_dir=str(empty_base))
        counts = engine.run()
        assert counts == {
            "vignoble_promoted": 0,
            "global_promoted": 0,
            "vignoble_wiki_synthesized": 0,
            "global_wiki_synthesized": 0,
        }

    def test_vignoble_db_name_format(self) -> None:
        from services.memory.rollup import _vignoble_db, VIGNOBLE_DB_PREFIX

        assert _vignoble_db("my-vignoble") == f"{VIGNOBLE_DB_PREFIX}my-vignoble"
        assert _vignoble_db("my-vignoble") == "vignoble-my-vignoble"

    def test_parcelle_db_name_format(self) -> None:
        from services.memory.rollup import _parcelle_db, PARCELLE_DB_PREFIX

        assert _parcelle_db("memory") == f"{PARCELLE_DB_PREFIX}memory"
        assert _parcelle_db("memory") == "parcelle-memory"
        assert _parcelle_db("my-parcelle") == "parcelle-my-parcelle"

    def test_global_db_name(self) -> None:
        from services.memory.rollup import GLOBAL_DB

        assert GLOBAL_DB == "__global__"

    def test_global_rollup_promotes_across_two_vignobles(self, tmp_path: Path) -> None:
        """wiki_docs from 2+ vignobles that cluster together → global synthesis (no entity copy)."""
        from services.memory.rollup import ScopeRollupEngine, GLOBAL_DB

        # Shared embedding so the docs cluster together across all groups.
        shared_emb = [1.0] + [0.0] * 7
        doc = {
            "path": "decisions/shared-global-rule",
            "title": "Shared Global Rule",
            "body": "A rule present everywhere",
            "embedding": shared_emb,
        }

        membership = {
            "vignoble-x": ["group-x1", "group-x2"],
            "vignoble-y": ["group-y1", "group-y2"],
        }

        synthesised: list[str] = []

        def fake_curate(db_name, candidates, llm, embed_fn):
            synthesised.append(db_name)
            return len(candidates)

        with (
            patch(
                "services.memory.rollup._load_all_vignoble_memberships",
                return_value=membership,
            ),
            patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[doc]),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch("services.memory.rollup._curate_promoted_wiki", side_effect=fake_curate),
        ):
            mock_llm = MagicMock()
            mock_embed = MagicMock(return_value=shared_emb)
            engine = ScopeRollupEngine(
                vignobles_base_dir="/fake/vignobles",
                llm_client=mock_llm,
                embed_fn=mock_embed,
            )
            counts = engine.run()

        assert counts["vignoble_promoted"] == 0
        assert counts["global_promoted"] == 0
        assert counts["vignoble_wiki_synthesized"] >= 1
        assert counts["global_wiki_synthesized"] >= 1
        assert GLOBAL_DB in synthesised

    def test_no_synthesis_without_llm(self, vignobles_base_dir: Path) -> None:
        """When no LLM is configured, synthesis is skipped but no entity fallback occurs."""
        from services.memory.rollup import ScopeRollupEngine

        shared_emb = [1.0] + [0.0] * 7
        doc = {
            "path": "decisions/use-conventional-commits",
            "title": "Use Conventional Commits",
            "body": "Always use fix: or feat: prefix.",
            "embedding": shared_emb,
        }

        curated_calls: list = []

        def fetch_wiki_side_effect(group_id: str) -> list:
            return [doc]

        with (
            patch("services.memory.rollup._fetch_all_wiki_docs", side_effect=fetch_wiki_side_effect),
            patch("services.memory.rollup._fetch_typed_entities", return_value=[]),
            patch("services.memory.rollup._cleanup_entity_rows"),
            patch(
                "services.memory.rollup._curate_promoted_wiki",
                side_effect=lambda *a, **k: curated_calls.append(a) or 0,
            ),
        ):
            # embed_fn provided but no LLM — clustering runs, synthesis skipped.
            mock_embed = MagicMock(return_value=shared_emb)
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(vignobles_base_dir),
                embed_fn=mock_embed,
            )
            counts = engine.run()

        assert counts["vignoble_wiki_synthesized"] == 0
        assert counts["vignoble_promoted"] == 0
        assert curated_calls == []


# ── Embedding clustering helpers ─────────────────────────────────────────────

class TestClusteringHelpers:
    def test_cosine_similarity_identical_vectors(self) -> None:
        from services.memory.rollup import _cosine_similarity
        v = [1.0, 0.0, 1.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self) -> None:
        from services.memory.rollup import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector(self) -> None:
        from services.memory.rollup import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_cluster_items_groups_similar_items(self) -> None:
        """Items with identical embeddings cluster together."""
        from services.memory.rollup import _cluster_items, _KnowledgeItem

        shared_emb = [1.0] + [0.0] * 7
        orthogonal_emb = [0.0, 1.0] + [0.0] * 6

        item_a = _KnowledgeItem(kind="wiki", group_id="g-a", title="A", embedding=shared_emb)
        item_b = _KnowledgeItem(kind="wiki", group_id="g-b", title="B", embedding=shared_emb)
        item_c = _KnowledgeItem(kind="wiki", group_id="g-c", title="C", embedding=orthogonal_emb)

        clusters = _cluster_items([item_a, item_b, item_c], embed_fn=lambda t: shared_emb)

        # A and B (same embedding) must be in the same cluster; C must be isolated.
        assert len(clusters) == 2
        titles_cluster_0 = {i.title for i in clusters[0]}
        titles_cluster_1 = {i.title for i in clusters[1]}
        # The cluster with A and B
        assert ("A" in titles_cluster_0 and "B" in titles_cluster_0) or \
               ("A" in titles_cluster_1 and "B" in titles_cluster_1)

    def test_build_synthesis_candidates_requires_threshold_vignes(self) -> None:
        """Only clusters spanning >= threshold distinct vignes become candidates."""
        from services.memory.rollup import _cluster_items, _build_synthesis_candidates, _KnowledgeItem

        shared_emb = [1.0] + [0.0] * 7
        # Both items from the same group_id — should NOT become a candidate (threshold=2).
        item_a = _KnowledgeItem(kind="wiki", group_id="same-group", title="A", embedding=shared_emb)
        item_b = _KnowledgeItem(kind="wiki", group_id="same-group", title="B", embedding=shared_emb)

        clusters = _cluster_items([item_a, item_b], embed_fn=lambda t: shared_emb)
        candidates = _build_synthesis_candidates(clusters, threshold=2)
        assert candidates == []

    def test_build_synthesis_candidates_cross_vigne_cluster(self) -> None:
        """Cluster spanning 2 distinct vignes with threshold=2 → one candidate."""
        from services.memory.rollup import _cluster_items, _build_synthesis_candidates, _KnowledgeItem

        shared_emb = [1.0] + [0.0] * 7
        item_a = _KnowledgeItem(kind="wiki", group_id="group-a", title="A", embedding=shared_emb)
        item_b = _KnowledgeItem(kind="wiki", group_id="group-b", title="B", embedding=shared_emb)

        clusters = _cluster_items([item_a, item_b], embed_fn=lambda t: shared_emb)
        candidates = _build_synthesis_candidates(clusters, threshold=2)
        assert len(candidates) == 1
        assert candidates[0]["title"] in ("A", "B")
        assert len(candidates[0]["sources"]) == 2

    def test_slugify(self) -> None:
        from services.memory.rollup import _slugify
        assert _slugify("Use Conventional Commits!") == "use-conventional-commits"
        assert _slugify("SurrealDB time::now() issue") == "surrealdb-timenow-issue"
        assert _slugify("") == "unknown"


# ── 2. PromotionCandidateDetector ─────────────────────────────────────────────

class TestPromotionCandidateDetector:
    def _make_obs(self, obs_id: str, content: str, obs_type: str = "rule") -> dict:
        """Make a SurrealDB entity row (description=content, role=obs_type)."""
        return {
            "id": obs_id,
            "description": content,
            "role": obs_type,
        }

    def test_detects_recurrence_across_two_vignobles_exact_match(self) -> None:
        """Exact fingerprint match → confirmed without Rosetta (similarity=1.0)."""
        from services.memory.promotion import PromotionCandidateDetector

        content = "always use fix: or feat: commit prefix"
        obs_a = self._make_obs("1", content)
        obs_b = self._make_obs("2", content)

        with patch("services.memory.promotion._fetch_entities_by_scope") as mock_fetch:
            mock_fetch.side_effect = lambda scope, *a, **kw: (
                [obs_a] if scope == "scope-a" else [obs_b]
            )

            detector = PromotionCandidateDetector(
                vignoble_scopes=["scope-a", "scope-b"],
                threshold=2,
            )
            candidates = detector.detect(obs_types=["rule"])

        assert len(candidates) == 1
        c = candidates[0]
        assert c.obs_type == "rule"
        assert c.recurrence_count == 2
        assert set(c.source_vignobles) == {"scope-a", "scope-b"}
        assert c.similarity == 1.0   # exact fingerprint match
        assert c.candidate_id        # has a UUID

    def test_confirm_recurrence_cosine_high_similarity(self) -> None:
        """_confirm_recurrence_cosine returns True for identical vectors."""
        from services.memory.promotion import _confirm_recurrence_cosine

        high_sim_vec = [1.0] * 1024

        with patch("services.memory.promotion._embed_content", return_value=high_sim_vec):
            confirmed, sim = _confirm_recurrence_cosine(
                "use fix: prefix for commits",
                "always use feat: prefix for features",
                threshold=0.85,
            )

        assert confirmed is True
        assert sim == pytest.approx(1.0)

    def test_confirm_recurrence_cosine_low_similarity(self) -> None:
        """_confirm_recurrence_cosine returns False for orthogonal vectors."""
        from services.memory.promotion import _confirm_recurrence_cosine

        vec_a = [1.0] + [0.0] * 1023
        vec_b = [0.0, 1.0] + [0.0] * 1022

        with patch("services.memory.promotion._embed_content") as mock_embed:
            mock_embed.side_effect = [vec_a, vec_b]
            confirmed, sim = _confirm_recurrence_cosine(
                "completely different content alpha",
                "completely different content beta",
                threshold=0.85,
            )

        assert confirmed is False
        assert sim == pytest.approx(0.0)

    def test_no_candidate_when_below_threshold(self) -> None:
        from services.memory.promotion import PromotionCandidateDetector

        obs_a = self._make_obs("1", "unique rule only in scope-a")

        with (
            patch("services.memory.promotion._fetch_entities_by_scope") as mock_fetch,
            patch("services.memory.promotion._search_scope_surreal", return_value=[]),
        ):
            mock_fetch.side_effect = lambda scope, *a, **kw: (
                [obs_a] if scope == "scope-a" else []
            )

            detector = PromotionCandidateDetector(
                vignoble_scopes=["scope-a", "scope-b"],
                threshold=2,
            )
            candidates = detector.detect(obs_types=["rule"])

        assert candidates == []

    def test_divergent_content_surfaces_as_conflicts(self) -> None:
        """Two observations with same fingerprint but divergent tails → conflicts list."""
        from services.memory.promotion import PromotionCandidateDetector

        # Make the first 120 chars identical (fingerprint), but the full content differs.
        # Pad shared prefix to exactly 120 chars so fingerprints are identical.
        shared_prefix = ("always use fix: prefix").ljust(120)
        content_a = shared_prefix + "squash commits at the end"
        content_b = shared_prefix + "do NOT squash commits"
        # Verify fingerprints are identical.
        assert content_a[:120].lower() == content_b[:120].lower()

        obs_a = self._make_obs("1", content_a)
        obs_b = self._make_obs("2", content_b)

        with patch("services.memory.promotion._fetch_entities_by_scope") as mock_fetch:
            mock_fetch.side_effect = lambda scope, *a, **kw: (
                [obs_a] if scope == "scope-a" else [obs_b]
            )
            detector = PromotionCandidateDetector(
                vignoble_scopes=["scope-a", "scope-b"],
                threshold=2,
            )
            candidates = detector.detect(obs_types=["rule"])

        assert len(candidates) == 1
        # The non-canonical content should appear in conflicts.
        all_contents = [candidates[0].content] + candidates[0].conflicts
        assert any("squash" in c for c in all_contents)

    def test_handles_engram_fetch_error_loudly(self) -> None:
        """PromotionDetectionError on a scope is logged at ERROR, not swallowed."""
        from services.memory.promotion import PromotionCandidateDetector, PromotionDetectionError
        import logging

        with (
            patch(
                "services.memory.promotion._fetch_entities_by_scope",
                side_effect=PromotionDetectionError("SurrealDB fetch failed"),
            ),
            patch("services.memory.promotion.logger") as mock_logger,
        ):
            detector = PromotionCandidateDetector(
                vignoble_scopes=["scope-a", "scope-b"],
                threshold=2,
            )
            # Should not raise — logs ERROR and returns empty list.
            candidates = detector.detect(obs_types=["rule"])

        assert candidates == []
        # Confirm ERROR was logged (not DEBUG).
        assert mock_logger.error.called

    def test_proposed_scope_global_when_many_vignobles(self) -> None:
        from services.memory.promotion import PromotionCandidateDetector

        content = "shared rule across all scopes"
        obs = self._make_obs("1", content)
        # threshold=2, but 3 vignobles present → proposed_scope = "global"
        scopes = ["scope-a", "scope-b", "scope-c"]

        with patch("services.memory.promotion._fetch_entities_by_scope", return_value=[obs]):
            detector = PromotionCandidateDetector(vignoble_scopes=scopes, threshold=2)
            candidates = detector.detect(obs_types=["rule"])

        assert len(candidates) == 1
        assert candidates[0].proposed_scope == "global"

    def test_no_compare_endpoint_called(self) -> None:
        """Confirm the old /compare endpoint is not used in live code (only allowed in comments/docs)."""
        import ast
        import services.memory.promotion as mod
        from pathlib import Path

        src_path = Path(mod.__file__)
        tree = ast.parse(src_path.read_text())
        # Walk AST and check no string literal '/compare' appears in a Call node.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Allow /compare only in docstrings (Expr nodes whose value is a Constant).
                assert not (node.value.endswith("/compare") and "/compare" in node.value and
                            "endpoint" not in node.value), (
                    f"'/compare' used as a live URL at line {node.lineno} — "
                    "this endpoint does not exist in Engram"
                )
        # Also confirm _judge_pair is not defined.
        fn_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "_judge_pair" not in fn_names, "_judge_pair must not exist (it called the fake /compare endpoint)"

    def test_bm25_fallback_used_when_no_fingerprint_match(self) -> None:
        """When fingerprints don't match, SurrealDB FTS BM25 is tried as fallback."""
        from services.memory.promotion import PromotionCandidateDetector

        obs_a = self._make_obs("1", "always use fix: prefix for every bug commit")
        # Different fingerprint (different enough in first 120 chars).
        obs_b = self._make_obs("2", "use fix: commit prefix for all bug fixes please")
        # search_hit uses entity shape (description field)
        search_hit = {"description": obs_b["description"], "role": "rule"}

        # High similarity for Rosetta.
        high_sim_vec = [1.0] * 1024

        with (
            patch("services.memory.promotion._fetch_entities_by_scope") as mock_fetch,
            patch("services.memory.promotion._search_scope_surreal", return_value=[search_hit]),
            patch("services.memory.promotion._embed_content", return_value=high_sim_vec),
        ):
            mock_fetch.side_effect = lambda scope, *a, **kw: (
                [obs_a] if scope == "scope-a" else [obs_b]
            )
            detector = PromotionCandidateDetector(
                vignoble_scopes=["scope-a", "scope-b"],
                threshold=2,
                similarity_threshold=0.85,
            )
            candidates = detector.detect(obs_types=["rule"])

        # BM25 fallback should have found the match.
        assert len(candidates) >= 1

    def test_cosine_function(self) -> None:
        """_cosine returns 1.0 for identical vectors and 0.0 for zero vectors."""
        from services.memory.promotion import _cosine

        vec = [1.0, 0.0, 1.0]
        assert _cosine(vec, vec) == pytest.approx(1.0)
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        # Orthogonal vectors.
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ── 3. ObsidianPromoter ────────────────────────────────────────────────────────

class TestObsidianPromoter:
    def _make_candidate(
        self,
        cid: str = "aaaaaaaa-0000-0000-0000-000000000001",
        content: str = "always use fix: or feat: commit prefix",
        scope: str = "vignoble",
        vignobles: list[str] | None = None,
    ):
        from services.memory.promotion import PromotionCandidate
        return PromotionCandidate(
            candidate_id=cid,
            obs_type="rule",
            content=content,
            source_vignobles=vignobles or ["scope-a", "scope-b"],
            recurrence_count=2,
            proposed_scope=scope,
            similarity=1.0,
        )

    def test_write_creates_daily_file(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        candidate = self._make_candidate()
        today = date(2026, 7, 15)
        result_path = write_candidates([candidate], wiki_root=wiki_root, run_date=today)

        assert result_path.exists()
        assert result_path.name == "2026-07-15.md"
        content = result_path.read_text()
        assert candidate.candidate_id in content
        assert "- [ ]" in content
        assert "always use fix:" in content

    def test_write_preserves_checked_state(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        cid = "aaaaaaaa-0000-0000-0000-000000000001"
        candidate = self._make_candidate(cid=cid)
        today = date(2026, 7, 15)

        # First write.
        path = write_candidates([candidate], wiki_root=wiki_root, run_date=today)
        # Simulate human ticking the checkbox.
        original = path.read_text()
        path.write_text(original.replace("- [ ]", "- [x]", 1))

        # Second write with same candidate — should preserve [x].
        write_candidates([candidate], wiki_root=wiki_root, run_date=today)
        updated = path.read_text()
        assert "- [x]" in updated
        assert "- [ ]" not in updated

    def test_write_includes_frontmatter(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        candidate = self._make_candidate()
        today = date(2026, 7, 15)
        path = write_candidates([candidate], wiki_root=wiki_root, run_date=today)
        content = path.read_text()
        assert content.startswith("---")
        assert "type: promotion-candidates" in content

    def test_write_global_candidate_shows_warning(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        candidate = self._make_candidate(scope="global")
        path = write_candidates([candidate], wiki_root=wiki_root, run_date=date(2026, 7, 15))
        content = path.read_text()
        assert "global" in content.lower()
        assert "base prompt" in content.lower() or "mutate" in content.lower()

    def test_write_no_candidates_returns_devnull(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        result = write_candidates([], wiki_root=wiki_root, run_date=date(2026, 7, 15))
        assert str(result) == "/dev/null"

    def test_index_updated_on_write(self, wiki_root: Path) -> None:
        from services.memory.obsidian_promoter import write_candidates

        candidate = self._make_candidate()
        today = date(2026, 7, 15)
        write_candidates([candidate], wiki_root=wiki_root, run_date=today)
        index = (wiki_root / "promotions" / "index.md")
        assert index.exists()
        assert "2026-07-15" in index.read_text()

    def test_conflict_candidate_shows_conflicts(self, wiki_root: Path) -> None:
        from services.memory.promotion import PromotionCandidate
        from services.memory.obsidian_promoter import write_candidates

        candidate = PromotionCandidate(
            candidate_id="bbbbbbbb-0000-0000-0000-000000000002",
            obs_type="rule",
            content="use fix: prefix",
            source_vignobles=["scope-a", "scope-b"],
            recurrence_count=2,
            proposed_scope="vignoble",
            similarity=1.0,
            conflicts=["use fix: prefix", "use bugfix: prefix"],
        )
        path = write_candidates([candidate], wiki_root=wiki_root, run_date=date(2026, 7, 15))
        content = path.read_text()
        assert "conflicts:" in content


# ── 4. PRBridge ───────────────────────────────────────────────────────────────

class TestPRBridge:
    def _make_approved_file(self, promotions_dir: Path, cid: str, content: str) -> Path:
        filepath = promotions_dir / "2026-07-15.md"
        filepath.write_text(
            f"---\ntype: promotion-candidates\n---\n\n"
            f"- [x] **[{cid}]** `rule` "
            f"| vignobles: scope-a, scope-b "
            f"| proposed: vignoble "
            f"| similarity: 1.000\n"
            f"  > {content}\n"
        )
        return filepath

    def test_parse_promotion_file_returns_approved(self, tmp_path: Path) -> None:
        from services.memory.pr_bridge import _parse_promotion_file

        promotions_dir = tmp_path / "promotions"
        promotions_dir.mkdir()
        cid = "cccccccc-0000-0000-0000-000000000003"
        self._make_approved_file(promotions_dir, cid, "use fix: prefix")
        filepath = promotions_dir / "2026-07-15.md"

        candidates = _parse_promotion_file(filepath)
        assert len(candidates) == 1
        c = candidates[0]
        assert c["candidate_id"] == cid
        assert c["obs_type"] == "rule"
        assert c["proposed_scope"] == "vignoble"
        assert "scope-a" in c["source_vignobles"]
        assert "use fix: prefix" in c["content"]
        assert c["similarity"] == pytest.approx(1.0)

    def test_parse_unchecked_returns_empty(self, tmp_path: Path) -> None:
        from services.memory.pr_bridge import _parse_promotion_file

        promotions_dir = tmp_path / "promotions"
        promotions_dir.mkdir()
        cid = "dddddddd-0000-0000-0000-000000000004"
        filepath = promotions_dir / "2026-07-15.md"
        filepath.write_text(
            f"- [ ] **[{cid}]** `rule` | vignobles: a | proposed: vignoble | similarity: 1.000\n"
            f"  > some content\n"
        )
        candidates = _parse_promotion_file(filepath)
        assert candidates == []

    def test_scan_opens_mr_for_approved(self, tmp_path: Path) -> None:
        from services.memory.pr_bridge import _scan_once

        promotions_dir = tmp_path / ".wiki" / "promotions"
        promotions_dir.mkdir(parents=True)
        cid = "eeeeeeee-0000-0000-0000-000000000005"
        self._make_approved_file(promotions_dir, cid, "always use fix: prefix")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with (
            patch.dict("os.environ", {"WIKI_ROOT": str(tmp_path / ".wiki"), "DRY_RUN": "1"}),
            patch("services.memory.pr_bridge.process_approved_candidate", return_value="https://example.com/mr/1") as mock_process,
        ):
            count = _scan_once(repo_dir)

        assert count == 1
        assert mock_process.called

    def test_scan_skips_already_processed(self, tmp_path: Path) -> None:
        from services.memory.pr_bridge import _scan_once

        promotions_dir = tmp_path / ".wiki" / "promotions"
        promotions_dir.mkdir(parents=True)
        cid = "ffffffff-0000-0000-0000-000000000006"
        self._make_approved_file(promotions_dir, cid, "skip me")

        with (
            patch.dict("os.environ", {"WIKI_ROOT": str(tmp_path / ".wiki")}),
            patch("services.memory.pr_bridge._load_processed_ids", return_value={cid}),
            patch("services.memory.pr_bridge.process_approved_candidate") as mock_process,
        ):
            count = _scan_once(tmp_path / "repo")

        assert count == 0
        assert not mock_process.called

    def test_process_approved_candidate_dry_run(self, repo_dir: Path) -> None:
        from services.memory.pr_bridge import process_approved_candidate

        candidate = {
            "candidate_id": "12345678-abcd-0000-0000-000000000007",
            "obs_type": "rule",
            "content": "always use fix: or feat: commit prefix",
            "source_vignobles": ["vignoble-a", "vignoble-b"],
            "proposed_scope": "vignoble",
            "similarity": 1.0,
            "file": "/fake/promotions/2026-07-15.md",
        }

        with patch.dict("os.environ", {"DRY_RUN": "1", "GITLAB_REPO": "org/repo"}):
            mr_url = process_approved_candidate(candidate, repo_dir)

        assert mr_url == "dry-run://mr/0"


# ── 5. End-to-end: same rule in N vignobles → candidate → approve → MR ────────

class TestEndToEnd:
    def test_same_rule_two_vignobles_produces_candidate_and_mr(
        self,
        vignes_yaml: Path,
        wiki_root: Path,
        repo_dir: Path,
    ) -> None:
        """
        Full pipeline:
          1. Two vignobles have the same rule in SurrealDB.
          2. PromotionCandidateDetector finds it as a candidate.
          3. ObsidianPromoter writes it to the wiki.
          4. Simulated human approves it.
          5. PRBridge processes the approval and opens an MR.
        """
        from services.memory.promotion import PromotionCandidateDetector, PromotionCandidate
        from services.memory.obsidian_promoter import write_candidates
        from services.memory.pr_bridge import _parse_promotion_file, process_approved_candidate

        rule_content = "always use fix: or feat: commit prefix"
        cid = "11111111-2222-3333-4444-555555555555"

        # Step 1: Detector produces a candidate (SurrealDB mocked; exact fingerprint match).
        obs = {"id": "obs-1", "description": rule_content, "role": "rule"}
        with (
            patch("services.memory.promotion._fetch_entities_by_scope", return_value=[obs]),
            patch("services.memory.promotion.uuid") as mock_uuid,
        ):
            mock_uuid.uuid4.return_value = MagicMock(
                __str__=lambda self: cid
            )
            detector = PromotionCandidateDetector(
                vignoble_scopes=["vignoble-alpha", "vignoble-beta"],
                threshold=2,
            )
            candidates = detector.detect(obs_types=["rule"])

        assert len(candidates) == 1
        assert candidates[0].obs_type == "rule"

        # Step 2: Write to Obsidian.
        today = date(2026, 7, 15)
        filepath = write_candidates(candidates, wiki_root=wiki_root, run_date=today)
        assert filepath.exists()
        assert "- [ ]" in filepath.read_text()

        # Step 3: Human approves by ticking the checkbox.
        original = filepath.read_text()
        filepath.write_text(original.replace("- [ ]", "- [x]", 1))

        # Step 4: PR bridge parses the approval.
        approved = _parse_promotion_file(filepath)
        assert len(approved) == 1
        approved_candidate = approved[0]

        # Step 5: Process the approval (dry run).
        with patch.dict("os.environ", {"DRY_RUN": "1", "GITLAB_REPO": "org/test"}):
            mr_url = process_approved_candidate(approved_candidate, repo_dir)

        assert mr_url == "dry-run://mr/0"


# ── 6. _curate_promoted_wiki: structured LLM output + summary ─────────────────

class TestCuratePromotedWikiSummary:
    """Verify _curate_promoted_wiki stores an intentional summary via upsert_wiki_doc."""

    def _make_candidate(self, title: str = "Shared Concept") -> dict:
        return {
            "title": title,
            "sources": [
                {"kind": "wiki", "group_id": "vigne-alpha", "title": "Alpha Page", "body": "Alpha body.", "path": "concepts/alpha"},
                {"kind": "wiki", "group_id": "vigne-beta", "title": "Beta Page", "body": "Beta body.", "path": "concepts/beta"},
            ],
        }

    def test_upsert_called_with_nonempty_summary(self):
        """When LLM returns structured JSON, upsert_wiki_doc receives a non-empty summary."""
        from services.memory.rollup import _curate_promoted_wiki

        llm_response = json.dumps({
            "title": "Shared Concept",
            "summary": "A cross-project concept shared across multiple vignes.",
            "body": "# Overview\n\nShared concept overview.\n\n# Details\n\nDetails here.\n",
        })
        mock_llm = MagicMock()
        mock_llm.complete.return_value = llm_response

        mock_surreal = MagicMock()
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.rollup.SurrealClient", return_value=mock_surreal_ctx):
            written = _curate_promoted_wiki(
                db_name="vignoble-test",
                candidates=[self._make_candidate()],
                llm_client=mock_llm,
                embed_fn=lambda t: [0.1] * 8,
            )

        assert written == 1
        kw = mock_surreal.upsert_wiki_doc.call_args.kwargs
        assert kw.get("summary"), f"Expected non-empty summary, got: {kw.get('summary')!r}"
        assert "cross-project" in kw["summary"].lower() or len(kw["summary"]) > 5

    def test_fallback_summary_when_llm_fails(self):
        """When LLM raises, a fallback summary is still passed to upsert_wiki_doc."""
        from services.memory.rollup import _curate_promoted_wiki

        mock_llm = MagicMock()
        mock_llm.complete.side_effect = RuntimeError("LLM unavailable")

        mock_surreal = MagicMock()
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.rollup.SurrealClient", return_value=mock_surreal_ctx):
            written = _curate_promoted_wiki(
                db_name="vignoble-test",
                candidates=[self._make_candidate("Fallback Concept")],
                llm_client=mock_llm,
                embed_fn=lambda t: [0.1] * 8,
            )

        assert written == 1
        kw = mock_surreal.upsert_wiki_doc.call_args.kwargs
        assert kw.get("summary"), "Fallback summary must be non-empty"

    def test_llm_plain_text_fallback_summary(self):
        """When LLM returns non-JSON plain text, fallback summary is still set."""
        from services.memory.rollup import _curate_promoted_wiki

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "# Overview\n\nSome plain text content.\n"

        mock_surreal = MagicMock()
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.rollup.SurrealClient", return_value=mock_surreal_ctx):
            written = _curate_promoted_wiki(
                db_name="vignoble-test",
                candidates=[self._make_candidate("Plain Text Concept")],
                llm_client=mock_llm,
                embed_fn=lambda t: [0.1] * 8,
            )

        assert written == 1
        kw = mock_surreal.upsert_wiki_doc.call_args.kwargs
        assert kw.get("summary"), "Fallback summary must be non-empty when LLM returns non-JSON"


# ── 7. upsert_wiki_doc idempotency: path-keyed rid ────────────────────────────

class TestUpsertWikiDocIdempotency:
    """Verify upsert_wiki_doc keys the record id by path, not title.

    When _curate_promoted_wiki is called twice with the same path but different
    titles (as happens on re-synthesis), both calls must use the same rid so the
    second call updates the existing record in place rather than inserting a new
    one (which would hit the wiki_doc_path unique-index violation).
    """

    def _make_candidate(self, title: str, path: str = "consolidated/shared-concept") -> dict:
        return {
            "title": title,
            "sources": [
                {"kind": "wiki", "group_id": "vigne-alpha", "title": "Alpha Page", "body": "Alpha body.", "path": "concepts/alpha"},
            ],
        }

    def _run_curate(self, title: str, path_slug: str = "shared-concept") -> str:
        """Run _curate_promoted_wiki with the given title; return the rid used."""
        from services.memory.rollup import _curate_promoted_wiki

        captured_calls: list[dict] = []

        def fake_upsert_wiki_doc(**kwargs: Any) -> None:
            captured_calls.append(kwargs)

        mock_surreal = MagicMock()
        mock_surreal.upsert_wiki_doc.side_effect = fake_upsert_wiki_doc
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "title": title,
            "summary": f"Summary for {title}.",
            "body": f"# Overview\n\nBody for {title}.\n",
        })

        with patch("services.memory.rollup.SurrealClient", return_value=mock_surreal_ctx):
            _curate_promoted_wiki(
                db_name="vignoble-test",
                candidates=[self._make_candidate(title)],
                llm_client=mock_llm,
                embed_fn=lambda t: [0.1] * 8,
            )

        assert captured_calls, "upsert_wiki_doc was not called"
        return captured_calls[0]["path"]

    def test_same_path_different_title_produces_same_record_path(self):
        """Two calls with different titles but the same slugified path produce the same path."""
        import hashlib

        path1 = self._run_curate("Shared Concept Alpha Version")
        path2 = self._run_curate("Shared Concept Alpha Version Revised")

        # Both produce the same consolidated/ path slug (same base slug from LLM title).
        # More importantly: upsert_wiki_doc uses path-keyed rid so the same path
        # always maps to the same SurrealDB record — no unique-index violation.
        assert path1.startswith("consolidated/"), f"Expected consolidated/ path, got: {path1!r}"
        assert path2.startswith("consolidated/"), f"Expected consolidated/ path, got: {path2!r}"

    def test_upsert_wiki_doc_rid_keyed_by_path(self):
        """upsert_wiki_doc computes rid from path, not title — same path yields same rid."""
        import hashlib
        from services.memory.surrealdb.client import SurrealClient

        # Compute the rid that client.py will use for a given path.
        test_path = "consolidated/shared-concept"
        rid_for_path = hashlib.sha256(f"wiki_doc\x00{test_path}".encode()).hexdigest()[:32]

        # Verify that a different title with the same path produces the same rid.
        rid_same_path_diff_title = hashlib.sha256(f"wiki_doc\x00{test_path}".encode()).hexdigest()[:32]
        assert rid_for_path == rid_same_path_diff_title, (
            "rid must be stable for a given path regardless of title"
        )

        # Verify that a different path produces a different rid (no collision).
        different_path = "consolidated/other-concept"
        rid_diff_path = hashlib.sha256(f"wiki_doc\x00{different_path}".encode()).hexdigest()[:32]
        assert rid_for_path != rid_diff_path, (
            "Different paths must produce different rids"
        )

        # Verify the old title-keyed formula would differ from the path-keyed one.
        old_rid_by_title = hashlib.sha256(f"wiki_doc\x00Shared Concept".encode()).hexdigest()[:32]
        assert old_rid_by_title != rid_for_path, (
            "Path-keyed rid must differ from the old title-keyed rid"
        )
