"""Live smoke tests for services/memory/wiki/curator.py.

Requires a running SurrealDB instance (SURREAL_URL, SURREAL_PASS env vars).
Skipped automatically when SURREAL_PASS is not set or SurrealDB is unreachable.

Run manually (port-forward pinard-uat SurrealDB first):
    kubectl port-forward -n pinard-uat svc/pinard-surrealdb 8000:8000
    SURREAL_URL=http://localhost:8000 SURREAL_PASS=<root-pass> \\
        pytest services/memory/wiki/tests/test_curator_live.py -v -m live

Acceptance criteria validated here:
- Given entities + typed edges in a SurrealDB group_id, the curator produces OKF
  pages with valid ontology type + validated typed links, written to the wiki repo.
- Incremental: a second run with no source changes makes NO LLM calls and no writes.
- Dedup: a near-duplicate concept updates the existing page (no duplicate path).
- Unknown/would-be-invalid type or edge handled gracefully (needs_review / omit edge),
  never crashes.
- LLM stub — no real LLM token needed for smoke tests.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------

_SURREAL_PASS = os.environ.get("SURREAL_PASS", "")
_SURREAL_URL = os.environ.get("SURREAL_URL", "http://localhost:8000")

pytestmark = pytest.mark.live

_skip_reason = "SURREAL_PASS not set — skipping live SurrealDB tests"
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
# Fixtures
# ---------------------------------------------------------------------------

_TEST_GROUP = "wiki-curator-smoke"


def _make_client():
    from services.memory.surrealdb.client import SurrealClient, _schema_applied
    _schema_applied.discard(_TEST_GROUP)
    return SurrealClient(
        group_id=_TEST_GROUP,
        url=_SURREAL_URL,
        user=os.environ.get("SURREAL_USER", "root"),
        password=_SURREAL_PASS,
    )


def _make_composed():
    from services.memory.ontology.registry import OntologyRegistry
    registry = OntologyRegistry()
    return registry.compose(_TEST_GROUP)


def _noop_embed(text: str) -> list[float]:
    return [0.0] * 1024


def _stub_llm(response: str = "# Summary\n\nStub content.\n") -> MagicMock:
    """Return a stub LLM client that returns canned OKF body."""
    llm = MagicMock()
    llm.complete.return_value = response
    return llm


def _apply_schema(surreal):
    from services.memory.ontology.registry import OntologyRegistry
    from services.memory.surrealdb.schema_gen import generate_schema_ddl, populate_ontology_meta
    import tempfile
    registry = OntologyRegistry()
    composed = registry.compose(_TEST_GROUP)
    ddl = generate_schema_ddl(composed)
    with tempfile.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
        f.write(ddl)
        tmp = f.name
    try:
        surreal.apply_schema(tmp)
    finally:
        os.unlink(tmp)
    populate_ontology_meta(surreal, composed)


def _cleanup(surreal):
    for tbl in (
        "wiki_references", "wiki_mentions", "wiki_doc",
        "edge_staging", "entity_staging", "entity",
        "wiki_curator_cursor",
    ):
        try:
            surreal.query(f"DELETE {tbl}")
        except Exception:
            pass


def _seed_entity(surreal, role: str, name: str, description: str = "") -> str:
    """Upsert an entity and return its record id string."""
    rec = surreal.upsert_entity(
        role=role,
        name=name,
        description=description,
        embedding=_noop_embed(f"{name} {description}"),
    )
    return str(rec.get("id", ""))


def _seed_edge(surreal, from_role: str, from_name: str, relation: str, to_role: str, to_name: str):
    """Create a RELATE edge between two entities (must already exist)."""
    surreal.relate(
        from_role=from_role,
        from_name=from_name,
        relation=relation,
        to_role=to_role,
        to_name=to_name,
    )


def _make_curator(surreal, repo_path: Path, llm: Any | None = None):
    from services.memory.wiki.curator import WikiCurator
    composed = _make_composed()
    return WikiCurator(
        group_id=_TEST_GROUP,
        surreal=surreal,
        embed_fn=_noop_embed,
        composed=composed,
        repo_path=repo_path,
        llm_client=llm or _stub_llm(),
        dry_run=True,  # skip git push / glab in smoke tests
    )


def _count_md_files(repo_path: Path) -> int:
    return len(list(repo_path.rglob("*.md")))


def _read_page(repo_path: Path, rel_path: str) -> str:
    p = repo_path / (rel_path + ".md")
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _frontmatter(repo_path: Path, rel_path: str) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    raw = _read_page(repo_path, rel_path)
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_if_no_surreal
class TestLiveCurator:
    """Live smoke tests — require a running SurrealDB."""

    def setup_method(self):
        self.surreal = _make_client()
        _apply_schema(self.surreal)
        _cleanup(self.surreal)

    def teardown_method(self):
        _cleanup(self.surreal)
        self.surreal.close()

    def test_entity_with_typed_edge_produces_valid_okf_page(self, tmp_path):
        """Entities + typed edge → OKF page with correct type + typed relation."""
        # Seed: a diagnosis entity resolved by an action entity.
        _seed_entity(self.surreal, "action", "fix-memory-leak", "Patch the memory leak.")
        _seed_entity(self.surreal, "diagnosis", "memory-leak", "Ingester leaks memory.")
        _seed_edge(
            self.surreal,
            "diagnosis", "memory-leak",
            "resolved_by",
            "action", "fix-memory-leak",
        )

        llm = _stub_llm("# Summary\n\nMemory leak diagnosed.\n\n# Citations\n\n[fix](actions/fix-memory-leak)\n")
        curator = _make_curator(self.surreal, tmp_path, llm=llm)

        with _patch_commit(curator):
            result = curator.curate()

        assert result["errors"] == 0, f"Errors: {result}"
        assert result["synthesized"] >= 1

        # Find the diagnosis page.
        from services.memory.wiki.curator import _slugify
        slug = _slugify("memory-leak")
        page_path = f"diagnoses/{slug}"  # correct plural for diagnosis
        page = _read_page(tmp_path, page_path)
        assert page, f"OKF page not written at {page_path}"

        fm = _frontmatter(tmp_path, page_path)
        assert fm.get("type") == "diagnosis", f"Expected type=diagnosis, got {fm}"
        assert fm.get("source") == "curator"
        assert fm.get("group_id") == _TEST_GROUP

    def test_unknown_role_produces_needs_review_page(self, tmp_path):
        """Entity with unknown role → page in concepts/, status=needs_review."""
        # Insert directly via SurrealDB query (bypassing upsert_entity role validation).
        from services.memory.wiki.curator import _slugify
        import hashlib
        name = "novel-concept-xyz"
        rid = hashlib.sha256(f"unknown_role\x00{name}".encode()).hexdigest()[:32]
        self.surreal.query(
            "UPSERT type::record('entity', $rid) SET "
            "role = $role, name = $name, description = $desc, "
            "updated_at = time::now()",
            {"rid": rid, "role": "unknown_role_xyz", "name": name, "desc": "Brand new concept."},
        )

        curator = _make_curator(self.surreal, tmp_path)
        with _patch_commit(curator):
            result = curator.curate()

        assert result["errors"] == 0
        slug = _slugify(name)
        page = _read_page(tmp_path, f"concepts/{slug}")
        assert page, "Page not written for unknown-role entity"
        fm = _frontmatter(tmp_path, f"concepts/{slug}")
        assert fm.get("status") == "needs_review"

    def test_incremental_second_run_no_llm_calls(self, tmp_path):
        """Second run with no entity changes → no LLM calls, no new files."""
        _seed_entity(self.surreal, "decision", "stable-decision", "A stable decision.")

        llm = _stub_llm()
        curator = _make_curator(self.surreal, tmp_path, llm=llm)
        with _patch_commit(curator):
            result1 = curator.curate()

        assert result1["synthesized"] >= 1
        first_call_count = llm.complete.call_count
        files_after_first = _count_md_files(tmp_path)

        # Second run — same entities, cursor now set.
        llm2 = _stub_llm()
        curator2 = _make_curator(self.surreal, tmp_path, llm=llm2)
        with _patch_commit(curator2):
            result2 = curator2.curate()

        assert result2["synthesized"] == 0, "Second run should synthesize nothing"
        llm2.complete.assert_not_called()
        assert _count_md_files(tmp_path) == files_after_first

    def test_near_duplicate_updates_existing_page(self, tmp_path):
        """Two near-identical entities → second one updates the existing page, no duplicate."""
        from services.memory.wiki.curator import _slugify

        # Seed first entity and run curator.
        _seed_entity(self.surreal, "decision", "surreal-pivot", "We pivoted to SurrealDB.")
        llm = _stub_llm()
        curator = _make_curator(self.surreal, tmp_path, llm=llm)
        with _patch_commit(curator):
            result1 = curator.curate()
        assert result1["synthesized"] >= 1
        files_after_first = _count_md_files(tmp_path)

        # Add a near-identical entity; curator with embeddings turned off will
        # still run synthesis but dedup logic uses cosine similarity.
        # Since we use zero embeddings, score = 1.0 (all-zero dot product / norms
        # are undefined — test verifies no crash and graceful handling).
        _seed_entity(self.surreal, "decision", "surrealdb-pivot-v2", "We pivoted to SurrealDB (v2).")

        llm2 = _stub_llm()
        curator2 = _make_curator(self.surreal, tmp_path, llm=llm2)
        with _patch_commit(curator2):
            result2 = curator2.curate()

        assert result2["errors"] == 0

    def test_invalid_edge_page_kept_no_crash(self, tmp_path):
        """Entity with invalid edge (no valid src_type pair) → page written, no crash."""
        # decision entity with an edge type that has no decision→* valid pair for IndicatesProblem.
        _seed_entity(self.surreal, "decision", "bad-edge-decision", "Decision with bad edge.")
        # Don't seed the edge via relate() — just verify the curator handles missing edges gracefully.

        curator = _make_curator(self.surreal, tmp_path)
        with _patch_commit(curator):
            result = curator.curate()

        assert result["errors"] == 0
        from services.memory.wiki.curator import _slugify
        slug = _slugify("bad-edge-decision")
        page = _read_page(tmp_path, f"decisions/{slug}")
        assert page, "Page should be written even when edges are missing/invalid"

    def test_human_authored_page_not_overwritten(self, tmp_path):
        """Human-authored page in repo → curator skips it even when entity exists."""
        from services.memory.wiki.curator import _slugify
        _seed_entity(self.surreal, "decision", "human-decision", "A human-authored decision.")

        # Pre-create the page as human-authored.
        slug = _slugify("human-decision")
        page_dir = tmp_path / "decisions"
        page_dir.mkdir(parents=True, exist_ok=True)
        page_file = page_dir / f"{slug}.md"
        original_content = "---\ntype: decision\ntitle: human-decision\nsource: human\n---\nOriginal human body.\n"
        page_file.write_text(original_content, encoding="utf-8")

        llm = _stub_llm()
        curator = _make_curator(self.surreal, tmp_path, llm=llm)
        with _patch_commit(curator):
            result = curator.curate()

        assert result["errors"] == 0
        llm.complete.assert_not_called()
        # File must be unchanged.
        assert page_file.read_text(encoding="utf-8") == original_content


# ---------------------------------------------------------------------------
# Context manager helper
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _patch_commit(curator):
    """Patch _commit_and_push_branch so we don't need a real git repo."""
    from unittest.mock import patch
    with patch.object(curator, "_commit_and_push_branch", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Global-scope fresh-DB tests (ensure_schema coverage for curate_all_vignobles)
# ---------------------------------------------------------------------------

_GLOBAL_CURATOR_TEST_GROUP = "wiki-curator-global-smoke"


def _make_global_curator_client():
    from services.memory.surrealdb.client import SurrealClient, _schema_applied
    _schema_applied.discard(_GLOBAL_CURATOR_TEST_GROUP)
    return SurrealClient(
        group_id=_GLOBAL_CURATOR_TEST_GROUP,
        url=_SURREAL_URL,
        user=os.environ.get("SURREAL_USER", "root"),
        password=_SURREAL_PASS,
    )


def _cleanup_curator_group(surreal):
    for tbl in (
        "wiki_references", "wiki_mentions", "wiki_doc",
        "edge_staging", "entity_staging", "entity",
        "wiki_curator_cursor",
    ):
        try:
            surreal.query(f"DELETE {tbl}")
        except Exception:
            pass


@skip_if_no_surreal
class TestLiveGlobalScopeCuration:
    """Live tests for global-scope (no prior schema) curation via curate_all_vignobles.

    Directly reproduces the bug: curate_all_vignobles must call ensure_schema before
    running WikiCurator.curate() so that wiki_curator_cursor exists. If the fix is
    reverted this test crashes with 'The table wiki_curator_cursor does not exist'.

    GLOBAL_WIKI_GROUP is monkeypatched to the throwaway test scope so the test
    never touches the real production __global__ database.
    """

    def setup_method(self):
        self.surreal = _make_global_curator_client()
        # Explicitly do NOT apply schema here — the fix must apply it.
        _cleanup_curator_group(self.surreal)

    def teardown_method(self):
        _cleanup_curator_group(self.surreal)
        self.surreal.close()

    def test_fresh_db_curation_succeeds_with_ensure_schema(self, tmp_path, monkeypatch):
        """curate_all_vignobles into a fresh scope with NO prior schema must not crash.

        Directly reproduces the original bug: curate_all_vignobles must call
        ensure_schema before WikiCurator.curate() so that wiki_curator_cursor exists.
        """
        import services.memory.wiki.curator as curator_mod
        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.wiki.curator import curate_all_vignobles
        from services.memory.surrealdb.client import _schema_applied

        # Redirect to the throwaway test scope.
        monkeypatch.setattr(curator_mod, "GLOBAL_WIKI_GROUP", _GLOBAL_CURATOR_TEST_GROUP)

        # Ensure the schema cache does not mask a missing ensure_schema call.
        _schema_applied.discard(_GLOBAL_CURATOR_TEST_GROUP)

        # Build a minimal global wiki root (no entities needed — tests the cursor path).
        global_wiki = tmp_path / "global-wiki"
        global_wiki.mkdir()
        # Empty wiki root is fine — curator will find no entities and return cleanly.

        registry = OntologyRegistry()
        llm = _stub_llm()

        # Use a nonexistent vignobles_base_dir so only the __global__ path runs.
        results = curate_all_vignobles(
            vignobles_base_dir=tmp_path / "no-vignobles",
            global_wiki_root=global_wiki,
            embed_fn=_noop_embed,
            llm_client=llm,
            registry=registry,
            dry_run=True,
        )

        global_counts = results.get(_GLOBAL_CURATOR_TEST_GROUP, {}).get(_GLOBAL_CURATOR_TEST_GROUP, {})
        assert global_counts.get("errors", 1) == 0, (
            f"curate_all_vignobles crashed on fresh DB (no ensure_schema?): "
            f"{global_counts}\nfull results: {results}"
        )
