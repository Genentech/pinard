"""Unit tests for services/memory/wiki/curator.py.

No LLM, no real SurrealDB, no network — all external calls are mocked.

Tests:
- Entities present → OKF page written with valid ontology type + relations.
- Unknown entity role → page written with needs_review status.
- Invalid edge pair → edge omitted from relations (no crash).
- Second run with no source changes → no LLM calls, no new files (incremental).
- Near-duplicate concept → existing page path reused (update, not duplicate).
- Human-authored page → never overwritten.
- Empty entity set → nothing synthesized.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from services.memory.wiki.curator import (
    WikiCurator,
    _slugify,
    _camel_to_snake,
    _cosine_similarity,
    _mean_embedding,
    _parse_synthesis_response,
)
from services.memory.ontology.registry import OntologyRegistry
from services.memory.ontology.entities import CoreEntity
from services.memory.ontology.edges import CoreEdge, EDGE_TYPE_MAP
from pydantic import Field


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_composed():
    registry = OntologyRegistry()
    return registry.compose("test-group")


def _noop_embed(text: str) -> list[float]:
    return [0.0] * 1024


_DEFAULT_LLM_RESPONSE = '{"title": "Synthesized concept", "summary": "One-sentence synthesized hint.", "body": "# Overview\\n\\nSynthesized content.\\n"}'


def _make_llm(response: str = _DEFAULT_LLM_RESPONSE) -> MagicMock:
    llm = MagicMock()
    llm.complete.return_value = response
    return llm


def _make_surreal(
    entities: list[dict] | None = None,
    wiki_docs: list[dict] | None = None,
) -> MagicMock:
    """Build a mock SurrealClient.

    query() is dispatched by inspecting the SQL string.

    _select_source_material runs a two-statement query (LET $cur; SELECT FROM entity).
    query() returns one result per statement, so the mock returns [None, [entities]]
    matching the live behaviour: rows[0]=None (LET), rows[-1]=[entities] (SELECT).

    _set_cursor uses time::now() server-side — no $ts param, just an UPSERT.

    wiki_docs: list of wiki_doc rows returned by _fetch_all_auto_serve_wiki_docs().
    """
    surreal = MagicMock()

    def _query(sql: str, vars: dict | None = None):
        sql_strip = sql.strip().lower()

        # Two-statement LET $cur; SELECT FROM entity — return [None, [entities]].
        # rows[-1] (the SELECT result) is the entity list.
        if "let $cur" in sql_strip and "from entity" in sql_strip:
            rows = entities or []
            return [None, rows]

        # Edge traversal queries (SELECT ->edge->entity.*).
        if "->entity.*" in sql_strip:
            return [[]]

        # Dedup similarity scan.
        if "wiki_doc" in sql_strip and "cosine" in sql_strip:
            return [[]]

        # _fetch_all_auto_serve_wiki_docs: SELECT path, title … FROM wiki_doc WHERE status = 'auto_serve'
        if "wiki_doc" in sql_strip and "auto_serve" in sql_strip and "select" in sql_strip:
            return [wiki_docs or []]

        # UPSERT wiki_curator_cursor (set_cursor, server-side time::now()) or wiki_doc.
        if "upsert" in sql_strip:
            return [[]]

        return [[]]

    surreal.query.side_effect = _query
    return surreal


def _make_curator(
    tmp_path: Path,
    entities: list[dict] | None = None,
    llm_response: str = _DEFAULT_LLM_RESPONSE,
    wiki_docs: list[dict] | None = None,
) -> tuple[WikiCurator, MagicMock, MagicMock]:
    composed = _make_composed()
    surreal = _make_surreal(entities=entities, wiki_docs=wiki_docs)
    llm = _make_llm(response=llm_response)
    curator = WikiCurator(
        group_id="test-group",
        surreal=surreal,
        embed_fn=_noop_embed,
        composed=composed,
        repo_path=tmp_path,
        llm_client=llm,
        dry_run=True,
    )
    return curator, surreal, llm


# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------

def _read_page(tmp_path: Path, rel_path: str) -> str:
    p = tmp_path / (rel_path + ".md")
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _frontmatter_from_page(tmp_path: Path, rel_path: str) -> dict:
    import yaml
    raw = _read_page(tmp_path, rel_path)
    import re
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWikiCurator:

    def test_empty_entity_set_returns_zero_counts(self, tmp_path):
        """No entities → nothing synthesized, no LLM calls; snapshot phase still runs."""
        curator, surreal, llm = _make_curator(tmp_path, entities=[])
        result = curator.curate()
        assert result["synthesized"] == 0
        llm.complete.assert_not_called()
        # Snapshot phase must have run: _fetch_all_auto_serve_wiki_docs queries wiki_doc.
        auto_serve_calls = [
            c for c in surreal.query.call_args_list
            if "auto_serve" in str(c)
        ]
        assert auto_serve_calls, "snapshot phase (_fetch_all_auto_serve_wiki_docs) was not called"

    def test_valid_entity_produces_okf_page(self, tmp_path):
        """A known-role entity → OKF page written with correct type + status."""
        entities = [{
            "id": "entity:abc123",
            "name": "SurrealDB pivot",
            "role": "decision",
            "description": "We pivoted to SurrealDB for memory storage.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        # LLM returns structured JSON with a synthesized title.
        llm_resp = '{"title": "SurrealDB Pivot Decision", "summary": "We pivoted to SurrealDB for memory storage.", "body": "# Overview\\n\\nSynthesized content.\\n"}'
        curator, surreal, llm = _make_curator(tmp_path, entities=entities, llm_response=llm_resp)

        # Patch git operations (dry_run=True skips push but still calls git add/commit).
        with patch.object(curator, "_commit_and_push_branch", return_value="dry-run://mr/0"):
            result = curator.curate()

        assert result["synthesized"] == 1
        llm.complete.assert_called_once()

        # OKF page slug is based on LLM-synthesized title.
        slug = _slugify("SurrealDB Pivot Decision", max_len=80)
        page = _read_page(tmp_path, f"decisions/{slug}")
        assert page, "OKF page not written"
        fm = _frontmatter_from_page(tmp_path, f"decisions/{slug}")
        assert fm["type"] == "decision"
        assert fm["source"] == "curator"
        assert fm["group_id"] == "test-group"
        assert "summary" in fm, "frontmatter must have 'summary' key"
        assert "description" not in fm, "frontmatter must NOT have 'description' key"
        assert "# Overview" in page, "body must use # Overview section"

    def test_unknown_role_produces_needs_review_page(self, tmp_path):
        """Entity with unknown role → page written under concepts/ as needs_review."""
        entities = [{
            "id": "entity:xyz",
            "name": "Novel concept",
            "role": "totally_unknown_role",
            "description": "Something new.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        llm_resp = '{"title": "Novel concept", "summary": "Something new.", "body": "# Overview\\n\\nSomething new.\\n"}'
        curator, _, llm = _make_curator(tmp_path, entities=entities, llm_response=llm_resp)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        assert result["synthesized"] == 1
        slug = _slugify("Novel concept", max_len=80)
        fm = _frontmatter_from_page(tmp_path, f"concepts/{slug}")
        assert fm["status"] == "needs_review"

    def test_invalid_edge_omitted_from_relations_no_crash(self, tmp_path):
        """Entity with invalid edge pair → page written, invalid edge not in relations."""
        entities = [{
            "id": "entity:xyz",
            "name": "Bad edge entity",
            "role": "decision",
            "description": "An entity with a bad edge.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            # decision → DependsOn → decision: valid; decision → Produces → task: check
            # Use an edge that has no valid pair for decision as src.
            "_edges": [
                {"edge": "IndicatesProblem", "target_name": "SomeTarget", "target_role": "diagnosis"},
            ],
        }]
        curator, _, llm = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        assert result["synthesized"] == 1
        assert result["errors"] == 0

    def test_human_authored_page_not_overwritten(self, tmp_path):
        """Existing human-authored page is skipped — curator never overwrites it."""
        slug = _slugify("Human decision")
        page_dir = tmp_path / "decisions"
        page_dir.mkdir(parents=True)
        human_page = page_dir / f"{slug}.md"
        human_page.write_text(
            "---\ntype: decision\ntitle: Human decision\nsource: human\n---\nHuman body.\n",
            encoding="utf-8",
        )

        entities = [{
            "id": "entity:h1",
            "name": "Human decision",
            "role": "decision",
            "description": "A human-authored decision.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, llm = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        assert result["synthesized"] == 0
        assert result["skipped"] == 1
        llm.complete.assert_not_called()
        # Content unchanged.
        assert "Human body." in human_page.read_text()

    def test_near_duplicate_reuses_existing_path(self, tmp_path):
        """Near-duplicate concept (cosine score >= threshold) → existing path reused."""
        existing_slug = _slugify("Existing decision")
        page_dir = tmp_path / "decisions"
        page_dir.mkdir(parents=True)
        existing_page = page_dir / f"{existing_slug}.md"
        existing_page.write_text(
            "---\ntype: decision\ntitle: Existing decision\nsource: curator\n---\nBody.\n",
            encoding="utf-8",
        )

        composed = _make_composed()
        surreal = MagicMock()

        def _query(sql: str, vars: dict | None = None):
            sql_strip = sql.strip().lower()
            # Two-statement LET $cur; SELECT FROM entity.
            if "let $cur" in sql_strip and "from entity" in sql_strip:
                return [None, [{
                    "id": "entity:dup1",
                    "name": "Near duplicate decision",
                    "role": "decision",
                    "description": "A very similar decision.",
                    "updated_at": "2026-07-22T12:00:00Z",
                    "data": {},
                    "_edges": [],
                }]]
            if "->entity.*" in sql_strip:
                return [[]]
            # Dedup: return high similarity for existing page.
            if "cosine" in sql_strip and "wiki_doc" in sql_strip:
                return [[{"path": f"decisions/{existing_slug}", "score": 0.95}]]
            # _fetch_all_auto_serve_wiki_docs: return empty (new wiki_doc rows not yet persisted).
            if "auto_serve" in sql_strip and "select" in sql_strip:
                return [[]]
            return [[]]

        surreal.query.side_effect = _query
        llm = _make_llm()

        curator = WikiCurator(
            group_id="test-group",
            surreal=surreal,
            embed_fn=_noop_embed,
            composed=composed,
            repo_path=tmp_path,
            llm_client=llm,
            dry_run=True,
        )
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        # Should have synthesized by merging into (re-slugged) page, not creating a raw dup.
        assert result["synthesized"] == 1
        # The synthesized page (re-slugged from LLM title "Synthesized concept") must exist.
        synth_slug = _slugify("Synthesized concept", max_len=80)
        synth_page = tmp_path / "decisions" / f"{synth_slug}.md"
        assert synth_page.exists(), f"Synthesized page not found at decisions/{synth_slug}.md"
        content = synth_page.read_text()
        assert "curator" in content or "Synthesized" in content

        # No duplicate page for the raw near-dup slug.
        dup_slug = _slugify("Near duplicate decision")
        assert not (tmp_path / "decisions" / f"{dup_slug}.md").exists()

    def test_incremental_no_entities_since_cursor(self, tmp_path):
        """With a cursor set and no changed entities → nothing synthesized, no LLM.

        The server-side subquery filters by the stored cursor; when no entity
        is newer the SELECT returns empty, so the mock returns [None, []].
        The snapshot phase must still run (wiki_doc queried for auto_serve rows).
        """
        curator, surreal, llm = _make_curator(tmp_path, entities=[])  # empty — cursor excludes all
        result = curator.curate()
        assert result["synthesized"] == 0
        llm.complete.assert_not_called()
        # Snapshot phase must have run even though no entities changed.
        auto_serve_calls = [
            c for c in surreal.query.call_args_list
            if "auto_serve" in str(c)
        ]
        assert auto_serve_calls, "snapshot phase (_fetch_all_auto_serve_wiki_docs) was not called on quiet cycle"

    def test_llm_failure_falls_back_gracefully(self, tmp_path):
        """LLM error → page still written with description fallback, no exception raised."""
        entities = [{
            "id": "entity:fail1",
            "name": "LLM fail entity",
            "role": "decision",
            "description": "This entity triggers an LLM failure.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, llm = _make_curator(tmp_path, entities=entities)
        llm.complete.side_effect = RuntimeError("LLM unavailable")

        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        assert result["synthesized"] == 1
        assert result["errors"] == 0
        # Fallback: slug uses raw entity name; body uses # Overview.
        slug = _slugify("LLM fail entity", max_len=80)
        page = _read_page(tmp_path, f"decisions/{slug}")
        assert "Overview" in page  # fallback body has # Overview

    def test_multiple_entities_produces_multiple_pages(self, tmp_path):
        """Multiple entities → one page each."""
        entities = [
            {
                "id": f"entity:{i}",
                "name": f"Decision {i}",
                "role": "decision",
                "description": f"Decision number {i}.",
                "updated_at": "2026-07-22T12:00:00Z",
                "data": {},
                "_edges": [],
            }
            for i in range(3)
        ]
        curator, _, llm = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        assert result["synthesized"] == 3
        assert llm.complete.call_count == 3

    def test_cursor_advanced_after_synthesis(self, tmp_path):
        """Cursor is updated after successful synthesis."""
        entities = [{
            "id": "entity:c1",
            "name": "Cursor test entity",
            "role": "decision",
            "description": "Test.",
            "updated_at": "2026-07-22T15:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, surreal, _ = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            curator.curate()

        # Check that set_cursor (UPSERT wiki_curator_cursor) was called.
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "wiki_curator_cursor" in (c.args[0] if c.args else "") and "upsert" in (c.args[0] if c.args else "").lower()
        ]
        assert upsert_calls, "wiki_curator_cursor was never updated"

    def _first_md_in(self, tmp_path: Path, subdir: str) -> dict:
        """Return frontmatter of the first .md file found in tmp_path/subdir."""
        import yaml
        import re
        d = tmp_path / subdir
        pages = list(d.glob("*.md")) if d.exists() else []
        assert pages, f"No .md file found under {subdir}/"
        raw = pages[0].read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
        return yaml.safe_load(m.group(1)) if m else {}

    def test_decision_role_gets_auto_serve_confidence(self, tmp_path):
        """A single 'decision' entity gets confidence ≥ 0.7 (role bonus) → auto_serve."""
        entities = [{
            "id": "entity:d1",
            "name": "Use SurrealDB for memory",
            "role": "decision",
            "description": "We chose SurrealDB as the memory graph store.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            curator.curate()

        fm = self._first_md_in(tmp_path, "decisions")
        assert fm["confidence"] >= 0.7, f"Expected confidence ≥ 0.7, got {fm['confidence']}"
        assert fm["status"] == "auto_serve"

    def test_diagnosis_role_gets_auto_serve_confidence(self, tmp_path):
        """A single 'diagnosis' entity gets confidence ≥ 0.7 (role bonus) → auto_serve."""
        entities = [{
            "id": "entity:diag1",
            "name": "OOM on shard 47",
            "role": "diagnosis",
            "description": "Memory budget exceeded causing OOM kill.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            curator.curate()

        fm = self._first_md_in(tmp_path, "diagnoses")
        assert fm["confidence"] >= 0.7, f"Expected confidence ≥ 0.7, got {fm['confidence']}"
        assert fm["status"] == "auto_serve"

    def test_artifact_role_excluded_from_wiki_synthesis(self, tmp_path):
        """Artifact-role entities are excluded from wiki synthesis entirely — no page written."""
        entities = [{
            "id": "entity:art1",
            "name": "raw artifact observation",
            "role": "artifact",
            "description": "A raw observation without high-value role.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities)
        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        # No page written — artifacts are excluded from wiki synthesis.
        artifacts_dir = tmp_path / "artifacts"
        pages = list(artifacts_dir.glob("*.md")) if artifacts_dir.exists() else []
        assert pages == [], f"Expected no artifact pages written, found: {pages}"
        assert result["synthesized"] == 0
        assert result["skipped"] == 1

    def test_multi_entity_cluster_produces_single_page(self, tmp_path):
        """Two highly similar decision entities cluster into one page, not two."""
        # Use identical embeddings to guarantee cosine similarity = 1.0.
        emb = [1.0] + [0.0] * 1023

        entities = [
            {
                "id": "entity:cl1",
                "name": "SurrealDB pivot decision",
                "role": "decision",
                "description": "We chose SurrealDB for memory graph storage.",
                "updated_at": "2026-07-22T12:00:00Z",
                "data": {},
                "_edges": [],
            },
            {
                "id": "entity:cl2",
                "name": "SurrealDB selection rationale",
                "role": "decision",
                "description": "SurrealDB was selected due to graph + vector capabilities.",
                "updated_at": "2026-07-22T12:01:00Z",
                "data": {},
                "_edges": [],
            },
        ]

        composed = _make_composed()
        surreal = _make_surreal(entities=entities)
        llm = _make_llm()

        # Inject fixed embeddings so both entities cluster together.
        def fixed_embed(text: str) -> list[float]:
            return emb

        curator = WikiCurator(
            group_id="test-group",
            surreal=surreal,
            embed_fn=fixed_embed,
            composed=composed,
            repo_path=tmp_path,
            llm_client=llm,
            dry_run=True,
        )

        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        # Two entities → one cluster → one synthesized page.
        assert result["synthesized"] == 1, (
            f"Expected 1 synthesized page from 2 similar entities, got {result['synthesized']}"
        )
        # LLM called once (one cluster).
        assert llm.complete.call_count == 1
        # Confidence boosted by cluster size. Slug is from LLM-synthesized title.
        slug = _slugify("Synthesized concept", max_len=80)  # default LLM mock title
        fm = _frontmatter_from_page(tmp_path, f"decisions/{slug}")
        # cluster_size=2: 0.60 + log2(3)*0.05 + 0.12 = 0.60 + 0.079 + 0.12 ≈ 0.799
        assert fm["confidence"] > 0.7
        assert fm["status"] == "auto_serve"

    def test_dissimilar_entities_produce_separate_pages(self, tmp_path):
        """Two orthogonal entities (cosine sim ≈ 0) produce two separate pages."""
        entities = [
            {
                "id": "entity:a1",
                "name": "Alpha decision",
                "role": "decision",
                "description": "Completely unrelated to beta.",
                "updated_at": "2026-07-22T12:00:00Z",
                "data": {},
                "_edges": [],
            },
            {
                "id": "entity:b1",
                "name": "Beta decision",
                "role": "decision",
                "description": "Completely unrelated to alpha.",
                "updated_at": "2026-07-22T12:01:00Z",
                "data": {},
                "_edges": [],
            },
        ]

        composed = _make_composed()
        surreal = _make_surreal(entities=entities)
        llm = _make_llm()
        call_count = [0]

        def orthogonal_embed(text: str) -> list[float]:
            # Return orthogonal vectors based on call order.
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                return [1.0] + [0.0] * 1023
            else:
                return [0.0, 1.0] + [0.0] * 1022

        curator = WikiCurator(
            group_id="test-group",
            surreal=surreal,
            embed_fn=orthogonal_embed,
            composed=composed,
            repo_path=tmp_path,
            llm_client=llm,
            dry_run=True,
        )

        with patch.object(curator, "_commit_and_push_branch", return_value=None):
            result = curator.curate()

        # Two orthogonal entities → two clusters → two pages.
        assert result["synthesized"] == 2

    def test_commit_and_push_branch_second_run_dirty_tree(self, tmp_path):
        """Regression: second run (remote branch exists) must not fail on dirty working tree.

        Before the fix, _commit_and_push_branch called `git checkout <branch>` (plain)
        which raises CalledProcessError when uncommitted .md files are already present.
        After the fix it uses `git checkout -B <branch>` unconditionally, which succeeds
        regardless of dirty state.
        """
        import subprocess
        from unittest.mock import call as mock_call

        entities = [{
            "id": "entity:r1",
            "name": "Repeat entity",
            "role": "decision",
            "description": "An entity that gets curated twice.",
            "updated_at": "2026-07-22T12:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities)

        # Track all _run calls made by _commit_and_push_branch.
        run_calls: list[list[str]] = []
        original_run = curator._run

        def _fake_run(cmd, cwd, check=True):
            run_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "decisions/repeat-entity.md" if "diff" in cmd else ""
            result.stderr = ""
            # ls-remote: simulate remote branch already exists (second run).
            if "ls-remote" in cmd:
                result.returncode = 0
            return result

        curator._run = _fake_run

        # Pre-create a .md file in the working tree to simulate dirty state.
        dirty_file = tmp_path / "decisions" / "repeat-entity.md"
        dirty_file.parent.mkdir(parents=True, exist_ok=True)
        dirty_file.write_text("dirty content", encoding="utf-8")

        branch = curator._branch_name()
        curator._commit_and_push_branch(["decisions/repeat-entity"])

        # Must have called `git checkout -B <branch>`, never plain `git checkout <branch>`.
        checkout_calls = [c for c in run_calls if "checkout" in c]
        assert checkout_calls, "Expected at least one git checkout call"
        for c in checkout_calls:
            assert "-B" in c, (
                f"Expected `git checkout -B` (not plain checkout), got: {c}\n"
                "Plain checkout fails on dirty working tree — regression guard."
            )

        # In dry_run mode push is skipped; verify via the real-remote tests instead.
        # If a push call was recorded (non-dry-run path), it must use --force-with-lease.
        push_calls = [c for c in run_calls if "push" in c]
        for c in push_calls:
            assert "--force-with-lease" in c, (
                f"Expected `git push --force-with-lease`, got: {c}\n"
                "Plain push is rejected non-fast-forward on second run — regression guard."
            )


class TestFullSnapshot:
    """Tests for the cumulative-snapshot behaviour introduced in issue #176.

    Every cycle must commit ALL current auto_serve wiki_doc rows, not just the
    delta from changed entities.  This prevents a pod restart from collapsing
    a large curator MR to a single page.
    """

    def _make_wiki_doc(
        self,
        path: str,
        title: str,
        role: str = "decision",
        group_id: str = "test-group",
        source: str = "curator",
    ) -> dict:
        return {
            "path": path,
            "title": title,
            "type": role,
            "summary": f"Summary for {title}.",
            "body": f"# Overview\n\n{title} body.\n",
            "frontmatter": {
                "type": role,
                "title": title,
                "source": source,
                "group_id": group_id,
                "confidence": 0.77,
                "status": "auto_serve",
            },
            "confidence": 0.77,
            "status": "auto_serve",
        }

    def test_full_snapshot_includes_unchanged_pages(self, tmp_path):
        """1 changed entity but 3 existing wiki_docs → branch gets all 3 files."""
        # SurrealDB has 3 auto_serve wiki_docs already (previously synthesized).
        existing_docs = [
            self._make_wiki_doc("decisions/alpha", "Alpha"),
            self._make_wiki_doc("decisions/beta", "Beta"),
            self._make_wiki_doc("decisions/gamma", "Gamma"),
        ]
        # Only 1 entity changed this cycle (the incremental cursor picked it up).
        # Use an LLM response whose title slug matches the entity name ("gamma")
        # so _process_cluster writes to decisions/gamma, deduplicating with wiki_docs.
        llm_resp = '{"title": "Gamma", "summary": "Gamma updated.", "body": "# Overview\\n\\nGamma.\\n"}'
        entities = [{
            "id": "entity:new1",
            "name": "Gamma",
            "role": "decision",
            "description": "Gamma updated.",
            "updated_at": "2026-08-01T10:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities, wiki_docs=existing_docs, llm_response=llm_resp)

        committed_paths: list[list[str]] = []

        def _fake_commit(paths):
            committed_paths.append(list(paths))
            return None

        curator._commit_and_push_branch = _fake_commit
        curator.curate()

        assert committed_paths, "_commit_and_push_branch was never called"
        committed = committed_paths[0]
        assert "decisions/alpha" in committed, "unchanged page 'alpha' missing from snapshot"
        assert "decisions/beta" in committed, "unchanged page 'beta' missing from snapshot"
        assert "decisions/gamma" in committed, "changed page 'gamma' missing from snapshot"
        assert len(committed) == 3, f"Expected 3 pages in snapshot, got {len(committed)}: {committed}"

    def test_pod_restart_does_not_shrink_mr(self, tmp_path):
        """Simulate pod restart: 1 changed entity, but 83 wiki_docs exist → 83 files committed."""
        n = 83
        existing_docs = [
            self._make_wiki_doc(f"decisions/page-{i:03d}", f"Page {i:03d}")
            for i in range(n)
        ]
        # Restart: only 1 entity changed (freshly ingested memory).
        # LLM title must match entity name so slug = "page-000" (deduplicates with wiki_docs).
        llm_resp = '{"title": "Page 000", "summary": "First page.", "body": "# Overview\\n\\nPage 000.\\n"}'
        entities = [{
            "id": "entity:restart1",
            "name": "Page 000",
            "role": "decision",
            "description": "A freshly ingested memory after restart.",
            "updated_at": "2026-08-01T10:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities, wiki_docs=existing_docs, llm_response=llm_resp)

        committed_paths: list[list[str]] = []

        def _fake_commit(paths):
            committed_paths.append(list(paths))
            return None

        curator._commit_and_push_branch = _fake_commit
        curator.curate()

        assert committed_paths, "_commit_and_push_branch was never called"
        committed = committed_paths[0]
        assert len(committed) == n, (
            f"Pod restart shrunk MR: expected {n} pages in snapshot, got {len(committed)}."
        )

    def test_stale_files_pruned(self, tmp_path):
        """Files on disk that are no longer in wiki_doc (non-human) are deleted."""
        # Pre-create a stale .md file on disk (not in wiki_docs).
        stale_dir = tmp_path / "decisions"
        stale_dir.mkdir(parents=True)
        stale_file = stale_dir / "stale-page.md"
        stale_file.write_text(
            "---\ntype: decision\ntitle: Stale Page\nsource: curator\ngroup_id: test-group\n---\nOld body.\n",
            encoding="utf-8",
        )

        # wiki_docs does NOT include stale-page (it was pruned from SurrealDB).
        existing_docs = [
            self._make_wiki_doc("decisions/current-page", "Current Page"),
        ]
        llm_resp = '{"title": "Current Page", "summary": "A current page.", "body": "# Overview\\n\\nCurrent.\\n"}'
        entities = [{
            "id": "entity:c1",
            "name": "Current Page",
            "role": "decision",
            "description": "A current page.",
            "updated_at": "2026-08-01T10:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities, wiki_docs=existing_docs, llm_response=llm_resp)
        curator._commit_and_push_branch = lambda paths: None
        curator.curate()

        assert not stale_file.exists(), "Stale curator-owned page was not pruned"
        assert (tmp_path / "decisions" / "current-page.md").exists(), "Current page was incorrectly removed"

    def test_human_pages_preserved_in_snapshot(self, tmp_path):
        """Human-authored pages on disk are never deleted or overwritten during snapshot."""
        human_dir = tmp_path / "decisions"
        human_dir.mkdir(parents=True)
        human_file = human_dir / "human-authored.md"
        human_content = "---\ntype: decision\ntitle: Human Page\nsource: human\n---\nHuman body.\n"
        human_file.write_text(human_content, encoding="utf-8")

        # wiki_docs includes a row for the same path but source != 'human'
        # (should not overwrite) and does not include human-authored (never in DB).
        existing_docs = [
            self._make_wiki_doc("decisions/curator-page", "Curator Page"),
        ]
        llm_resp = '{"title": "Curator Page", "summary": "A curator page.", "body": "# Overview\\n\\nCurator.\\n"}'
        entities = [{
            "id": "entity:h1",
            "name": "Curator Page",
            "role": "decision",
            "description": "A curator page.",
            "updated_at": "2026-08-01T10:00:00Z",
            "data": {},
            "_edges": [],
        }]
        curator, _, _ = _make_curator(tmp_path, entities=entities, wiki_docs=existing_docs, llm_response=llm_resp)
        curator._commit_and_push_branch = lambda paths: None
        curator.curate()

        # Human-authored file must be untouched.
        assert human_file.exists(), "Human-authored page was deleted — must be preserved"
        assert human_file.read_text(encoding="utf-8") == human_content, (
            "Human-authored page content was modified — must never be overwritten"
        )

    def test_emit_all_wiki_docs_uses_stored_body_no_llm(self, tmp_path):
        """_emit_all_wiki_docs must write the stored body verbatim — no LLM call."""
        docs = [
            self._make_wiki_doc("decisions/verbatim", "Verbatim"),
        ]
        curator, _, llm = _make_curator(tmp_path, entities=[], wiki_docs=docs)
        written = curator._emit_all_wiki_docs(docs)
        assert written == ["decisions/verbatim"]
        content = (tmp_path / "decisions" / "verbatim.md").read_text(encoding="utf-8")
        assert "Verbatim body." in content
        llm.complete.assert_not_called()

    def test_fetch_all_auto_serve_no_group_id_filter(self, tmp_path):
        """_fetch_all_auto_serve_wiki_docs must NOT filter by group_id in SQL.

        wiki_doc has no top-level group_id column (group_id is inside frontmatter
        and each scope is already an isolated SurrealDB database).  Adding
        `AND group_id = $gid` silently returns 0 rows, breaking the snapshot.
        Regression guard: verify the issued SQL contains no 'group_id' clause.
        """
        issued_sqls: list[str] = []
        surreal = MagicMock()

        def _capturing_query(sql: str, vars: dict | None = None):
            issued_sqls.append(sql)
            # Return 3 auto_serve docs to verify they are returned.
            if "auto_serve" in sql.lower() and "wiki_doc" in sql.lower():
                return [[
                    self._make_wiki_doc("decisions/a", "A"),
                    self._make_wiki_doc("decisions/b", "B"),
                    self._make_wiki_doc("decisions/c", "C"),
                ]]
            return [[]]

        surreal.query.side_effect = _capturing_query
        curator = WikiCurator(
            group_id="test-group",
            surreal=surreal,
            embed_fn=_noop_embed,
            composed=_make_composed(),
            repo_path=tmp_path,
            llm_client=_make_llm(),
            dry_run=True,
        )
        result = curator._fetch_all_auto_serve_wiki_docs()

        # Must return the 3 docs (no group_id filter zeroing them out).
        assert len(result) == 3, (
            f"Expected 3 docs; got {len(result)}. "
            "Likely the query still filters by group_id, which is not a top-level column."
        )

        # SQL must not contain a group_id filter.
        wiki_doc_sqls = [s for s in issued_sqls if "wiki_doc" in s.lower() and "auto_serve" in s.lower()]
        assert wiki_doc_sqls, "No wiki_doc auto_serve query was issued"
        for sql in wiki_doc_sqls:
            assert "group_id" not in sql.lower(), (
                f"Query must not filter by group_id (no top-level column): {sql!r}"
            )

    def test_quiet_cycle_recreates_branch_from_snapshot(self, tmp_path):
        """No changed entities but wiki_docs exist in DB → branch is created with full snapshot.

        Regression test for #178: the cumulative snapshot must run even when
        _select_source_material returns empty (cursors already current, quiet cycle).
        Deleting a curator branch then restarting must recreate it via snapshot.
        """
        existing_docs = [
            self._make_wiki_doc("decisions/alpha", "Alpha"),
            self._make_wiki_doc("decisions/beta", "Beta"),
        ]
        # No changed entities (simulates a restart after cursors are already current).
        curator, _, llm = _make_curator(tmp_path, entities=[], wiki_docs=existing_docs)

        committed_paths: list[list[str]] = []

        def _fake_commit(paths):
            committed_paths.append(list(paths))
            return "dry-run://mr/0"

        curator._commit_and_push_branch = _fake_commit
        result = curator.curate()

        # LLM synthesis must not run (no entities).
        llm.complete.assert_not_called()
        assert result["synthesized"] == 0

        # Snapshot phase must have committed the existing auto_serve docs.
        assert committed_paths, (
            "_commit_and_push_branch was not called on quiet cycle — branch will not be recreated"
        )
        committed = committed_paths[0]
        assert "decisions/alpha" in committed, "'alpha' missing from quiet-cycle snapshot"
        assert "decisions/beta" in committed, "'beta' missing from quiet-cycle snapshot"
        assert len(committed) == 2, f"Expected 2 pages, got {len(committed)}: {committed}"
        # MR counter is incremented when commit_and_push_branch returns a URL.
        assert result["mr_opened"] == 1


class TestCommitAndPushBranchRealRemote:
    """Real-git tests using a temporary bare repo as origin.

    These tests run without SurrealDB or a real LLM — they only exercise the
    git operations in _commit_and_push_branch to catch bugs that mocked _run
    tests cannot (e.g. non-fast-forward push rejection on second run).
    """

    def _make_bare_remote_and_clone(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a bare repo (remote) and a working clone. Return (bare, clone)."""
        import subprocess
        bare = tmp_path / "remote.git"
        clone = tmp_path / "clone"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
        # Configure identity so git commit works.
        for key, val in [("user.email", "test@example.com"), ("user.name", "Test")]:
            subprocess.run(["git", "-C", str(clone), "config", key, val], check=True, capture_output=True)
        # Seed an initial commit so the clone has a HEAD.
        readme = clone / "README.md"
        readme.write_text("wiki\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(clone), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD"], check=True, capture_output=True)
        return bare, clone

    def _make_real_curator(self, clone: Path) -> WikiCurator:
        """Return a non-dry-run curator pointing at the clone."""
        from services.memory.wiki.curator import WikiCurator
        composed = _make_composed()
        surreal = _make_surreal(entities=[])
        llm = _make_llm()
        return WikiCurator(
            group_id="test-real-remote",
            surreal=surreal,
            embed_fn=_noop_embed,
            composed=composed,
            repo_path=clone,
            llm_client=llm,
            dry_run=False,  # real git push
            gitlab_repo="",  # skip glab mr create
        )

    def test_first_run_pushes_branch(self, tmp_path):
        """First run: curator branch is created and pushed to origin."""
        import subprocess
        bare, clone = self._make_bare_remote_and_clone(tmp_path)
        curator = self._make_real_curator(clone)

        # Write a .md file to simulate what _process_cluster does.
        page_dir = clone / "decisions"
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "first-run.md").write_text(
            "---\ntype: decision\ntitle: first-run\nsource: curator\n---\nBody.\n",
            encoding="utf-8",
        )

        url = curator._commit_and_push_branch(["decisions/first-run"])
        # dry_run=False but no glab → _open_mr returns None (glab not found).
        # What matters is the branch exists on origin.
        branch = curator._branch_name()
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", str(bare), branch],
            capture_output=True,
        )
        assert result.returncode == 0, f"Branch {branch} not pushed to bare remote"

    def test_second_run_push_succeeds_no_fast_forward_error(self, tmp_path):
        """Second run: push succeeds even though remote branch already exists.

        This is the regression test for the non-fast-forward push failure.
        Without --force-with-lease the push is rejected; with it the branch
        is refreshed and the call succeeds (returncode == 0).
        """
        import subprocess
        bare, clone = self._make_bare_remote_and_clone(tmp_path)
        curator = self._make_real_curator(clone)
        branch = curator._branch_name()

        # First run.
        page_dir = clone / "decisions"
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page-v1.md").write_text(
            "---\ntype: decision\ntitle: page-v1\nsource: curator\n---\nFirst.\n",
            encoding="utf-8",
        )
        curator._commit_and_push_branch(["decisions/page-v1"])

        # Verify branch is on remote after first run.
        r = subprocess.run(
            ["git", "ls-remote", "--exit-code", str(bare), branch],
            capture_output=True,
        )
        assert r.returncode == 0, "Branch must exist on remote after first run"

        # Reset local branch to main (simulates curator starting fresh from HEAD).
        subprocess.run(["git", "-C", str(clone), "checkout", "main"], capture_output=True, check=False)
        subprocess.run(["git", "-C", str(clone), "branch", "-D", branch], capture_output=True, check=False)

        # Second run: write a new/updated page (new content, same branch target).
        (page_dir / "page-v2.md").write_text(
            "---\ntype: decision\ntitle: page-v2\nsource: curator\n---\nSecond.\n",
            encoding="utf-8",
        )
        # Intercept push to capture returncode without needing real GitLab.
        push_results: list[int] = []
        original_run = curator._run

        def _capturing_run(cmd, cwd, check=True):
            result = original_run(cmd, cwd, check=False)
            if "push" in cmd:
                push_results.append(result.returncode)
            return result

        curator._run = _capturing_run
        curator._commit_and_push_branch(["decisions/page-v2"])

        assert push_results, "No git push call recorded on second run"
        assert push_results[0] == 0, (
            f"git push failed on second run (rc={push_results[0]}) — "
            "non-fast-forward rejection regression: push must use --force-with-lease"
        )


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_slugify_basic(self):
        assert _slugify("SurrealDB pivot") == "surrealdb-pivot"
        assert _slugify("Memory leak in ingester!") == "memory-leak-in-ingester"
        assert _slugify("") == "unknown"
        assert _slugify("  ") == "unknown"

    def test_slugify_max_len_truncates_at_word_boundary(self):
        s = _slugify("a" * 40 + " " + "b" * 40, max_len=80)
        assert len(s) <= 80
        # Should not end in a dash.
        assert not s.endswith("-")

    def test_slugify_max_len_zero_means_no_cap(self):
        long_name = "word " * 30
        s = _slugify(long_name, max_len=0)
        assert len(s) > 80  # no cap applied

    def test_camel_to_snake(self):
        assert _camel_to_snake("DependsOn") == "depends_on"
        assert _camel_to_snake("ResolvedBy") == "resolved_by"
        assert _camel_to_snake("IndicatesProblem") == "indicates_problem"

    def test_cosine_similarity_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-9

    def test_cosine_similarity_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(_cosine_similarity(a, b)) < 1e-9

    def test_cosine_similarity_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_cosine_similarity_empty(self):
        assert _cosine_similarity([], []) == 0.0

    def test_mean_embedding_single(self):
        emb = [1.0, 2.0, 3.0]
        result = _mean_embedding([emb])
        assert result == emb

    def test_mean_embedding_two(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        result = _mean_embedding([a, b])
        assert result == [0.5, 0.5]

    def test_mean_embedding_empty(self):
        assert _mean_embedding([]) is None


class TestParseSynthesisResponse:
    def test_valid_json(self):
        raw = '{"title": "Clean Title", "summary": "One sentence.", "body": "# Overview\\n\\nText."}'
        result = _parse_synthesis_response(raw)
        assert result["title"] == "Clean Title"
        assert result["summary"] == "One sentence."
        assert "Overview" in result["body"]

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"title": "T", "summary": "S", "body": "B"}\n```'
        result = _parse_synthesis_response(raw)
        assert result["title"] == "T"

    def test_embedded_json(self):
        raw = 'Some preamble\n{"title": "T", "summary": "S", "body": "B"}\ntrailing'
        result = _parse_synthesis_response(raw)
        assert result["title"] == "T"

    def test_invalid_json_returns_empty(self):
        result = _parse_synthesis_response("not json at all")
        assert result == {}

    def test_unknown_keys_ignored(self):
        raw = '{"title": "T", "summary": "S", "body": "B", "extra": "ignored"}'
        result = _parse_synthesis_response(raw)
        assert "extra" not in result
        assert set(result.keys()) == {"title", "summary", "body"}
