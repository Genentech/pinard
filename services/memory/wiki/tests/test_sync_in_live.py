"""Live smoke tests for services/memory/wiki/sync_in.py.

Requires a running SurrealDB instance (SURREAL_URL, SURREAL_PASS env vars).
Skipped automatically when SURREAL_PASS is not set or SurrealDB is unreachable.

Run manually:
    SURREAL_URL=http://localhost:8000 SURREAL_PASS=root \\
        pytest services/memory/wiki/tests/test_sync_in_live.py -v

Acceptance criteria validated here (no LLM required):
- Drop a hand-written OKF page (valid type + a typed link) into the wiki
  repo → after sync it appears as wiki_doc (embedded) + a typed
  wiki_references edge, and is recall-visible (status honored).
- Unknown type → page ingested as needs_review, not dropped; searchable.
- Invalid edge pair → page kept, edge not materialized.
- Re-running sync with no file changes = no-op (idempotent).
- Changed file → re-sync → UPDATES in place (no duplicate, no unique
  violation).
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

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

# Try a quick connect probe at import time.
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

_TEST_GROUP = "wiki-smoke-test"


def _make_client():
    """Return a live SurrealClient for the test group_id."""
    from services.memory.surrealdb.client import SurrealClient, _schema_applied
    _schema_applied.discard(_TEST_GROUP)
    return SurrealClient(
        group_id=_TEST_GROUP,
        url=_SURREAL_URL,
        user=os.environ.get("SURREAL_USER", "root"),
        password=_SURREAL_PASS,
    )


def _make_composed():
    """Return a real ComposedOntology for the test group."""
    from services.memory.ontology.registry import OntologyRegistry
    registry = OntologyRegistry()
    return registry.compose(_TEST_GROUP)


def _noop_embed(text: str) -> list[float]:
    """Return a zero vector — no Rosetta needed for smoke tests."""
    return [0.0] * 1024


def _make_syncer(surreal, repo_path: Path):
    from services.memory.wiki.sync_in import WikiSyncer
    return WikiSyncer(
        group_id=_TEST_GROUP,
        surreal=surreal,
        embed_fn=_noop_embed,
        composed=_make_composed(),
        repo_path=repo_path,
    )


def _apply_schema(surreal):
    """Apply schema so wiki_doc / wiki_references / edge_staging tables exist."""
    from services.memory.ontology.registry import OntologyRegistry
    from services.memory.surrealdb.schema_gen import generate_schema_ddl, populate_ontology_meta
    import tempfile as _tmp
    registry = OntologyRegistry()
    composed = registry.compose(_TEST_GROUP)
    ddl = generate_schema_ddl(composed)
    with _tmp.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
        f.write(ddl)
        tmp = f.name
    try:
        surreal.apply_schema(tmp)
    finally:
        os.unlink(tmp)
    populate_ontology_meta(surreal, composed)


def _count_wiki_docs(surreal, path: str) -> int:
    rows = surreal.query("SELECT count() FROM wiki_doc WHERE path = $path GROUP ALL", {"path": path})
    if not rows or not rows[0]:
        return 0
    row = rows[0]
    row = row[0] if isinstance(row, list) else row
    return row.get("count", 0) if isinstance(row, dict) else 0


def _get_wiki_doc(surreal, path: str) -> dict | None:
    rows = surreal.query("SELECT * FROM wiki_doc WHERE path = $path LIMIT 1", {"path": path})
    if not rows or not rows[0]:
        return None
    row = rows[0]
    row = row[0] if isinstance(row, list) else row
    return row if isinstance(row, dict) else None


def _count_wiki_references(surreal, src_path: str) -> int:
    rid = hashlib.sha256(f"wiki_doc\x00{src_path}".encode()).hexdigest()[:32]
    rows = surreal.query(
        "SELECT count() FROM wiki_references WHERE in = type::record('wiki_doc', $rid) GROUP ALL",
        {"rid": rid},
    )
    if not rows or not rows[0]:
        return 0
    row = rows[0]
    row = row[0] if isinstance(row, list) else row
    return row.get("count", 0) if isinstance(row, dict) else 0


def _cleanup(surreal):
    """Remove all wiki_doc + wiki_references + edge_staging rows in the test DB."""
    for tbl in ("wiki_references", "wiki_mentions", "wiki_doc", "edge_staging"):
        try:
            surreal.query(f"DELETE {tbl}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_if_no_surreal
class TestLiveSyncIn:
    """Live smoke tests — require a running SurrealDB."""

    def setup_method(self):
        self.surreal = _make_client()
        _apply_schema(self.surreal)
        _cleanup(self.surreal)

    def teardown_method(self):
        _cleanup(self.surreal)
        self.surreal.close()

    def test_valid_page_ingested_with_typed_edge(self, tmp_path):
        """Valid OKF page → wiki_doc + typed wiki_references edge + recall-visible."""
        # Create target page first so the RELATE can resolve.
        # Use diagnosis → ResolvedBy → action which is a valid ontology pair.
        target_md = tmp_path / "actions" / "fix-memory-leak.md"
        target_md.parent.mkdir()
        target_md.write_text(textwrap.dedent("""\
            ---
            type: action
            title: Fix memory leak
            status: auto_serve
            ---
            Apply the memory leak patch.
        """))

        src_md = tmp_path / "diagnoses" / "memory-leak.md"
        src_md.parent.mkdir()
        src_md.write_text(textwrap.dedent("""\
            ---
            type: diagnosis
            title: Memory leak in ingester
            confidence: 0.9
            status: auto_serve
            relations:
              - edge: ResolvedBy
                to: actions/fix-memory-leak
            ---
            The ingester leaks memory on large batches.
        """))

        syncer = _make_syncer(self.surreal, tmp_path)

        # Sync target first, then source.
        assert syncer.sync_file(target_md) is True
        assert syncer.sync_file(src_md) is True

        # wiki_doc must exist with correct fields.
        doc = _get_wiki_doc(self.surreal, "diagnoses/memory-leak")
        assert doc is not None, "wiki_doc not found after sync"
        assert doc["type"] == "diagnosis"
        assert doc["status"] == "auto_serve"
        assert doc["path"] == "diagnoses/memory-leak"

        # Typed wiki_references edge must exist.
        edges = self.surreal.query(
            "SELECT edge_type FROM wiki_references WHERE "
            "in = type::record('wiki_doc', $rid)",
            {"rid": hashlib.sha256(b"wiki_doc\x00diagnoses/memory-leak").hexdigest()[:32]},
        )
        assert edges and edges[0], "wiki_references edge not created"
        edge_row = edges[0]
        edge_row = edge_row[0] if isinstance(edge_row, list) else edge_row
        assert edge_row.get("edge_type") == "ResolvedBy"

        # Recall visibility: doc must appear in a SELECT filtered by status.
        visible = self.surreal.query(
            "SELECT id FROM wiki_doc WHERE path = $path AND status = 'auto_serve' LIMIT 1",
            {"path": "diagnoses/memory-leak"},
        )
        assert visible and visible[0], "doc not recall-visible with status=auto_serve"

    def test_unknown_type_stored_as_needs_review_not_dropped(self, tmp_path):
        """Unknown type → needs_review, not dropped; recall-searchable."""
        md = tmp_path / "novel.md"
        md.write_text(textwrap.dedent("""\
            ---
            type: novel_concept_xyz
            title: Something new
            ---
            Body text here.
        """))
        syncer = _make_syncer(self.surreal, tmp_path)
        assert syncer.sync_file(md) is True

        doc = _get_wiki_doc(self.surreal, "novel")
        assert doc is not None, "page was dropped (should be stored as needs_review)"
        assert doc["status"] == "needs_review"
        assert doc["type"] == "novel_concept_xyz"

    def test_invalid_edge_page_kept_edge_not_materialized(self, tmp_path):
        """Invalid edge pair → page kept, edge not materialized."""
        # decision → DependsOn → task is not a valid pair for decision src
        # (DependsOn valid_pairs only contains (task, task) in core ontology).
        md = tmp_path / "bad-edge.md"
        md.write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Bad edge test
            relations:
              - edge: DependsOn
                to: some/task-page
            ---
            Body.
        """))
        syncer = _make_syncer(self.surreal, tmp_path)
        assert syncer.sync_file(md) is True

        # Page must exist.
        doc = _get_wiki_doc(self.surreal, "bad-edge")
        assert doc is not None, "page was dropped despite invalid edge"

        # No wiki_references edge created.
        assert _count_wiki_references(self.surreal, "bad-edge") == 0

    def test_idempotent_no_op_on_unchanged_file(self, tmp_path):
        """Re-running sync with no file change = no-op; no extra rows."""
        md = tmp_path / "stable.md"
        md.write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Stable page
            ---
            Unchanged content.
        """))
        syncer = _make_syncer(self.surreal, tmp_path)
        assert syncer.sync_file(md) is True
        assert _count_wiki_docs(self.surreal, "stable") == 1

        # Second sync — same content → skipped.
        syncer2 = _make_syncer(self.surreal, tmp_path)
        assert syncer2.sync_file(md) is False
        assert _count_wiki_docs(self.surreal, "stable") == 1  # still exactly 1 row

    def test_changed_file_updates_in_place_no_duplicate(self, tmp_path):
        """Changed file → re-sync updates the record in place; no duplicate, no UNIQUE violation."""
        md = tmp_path / "evolving.md"
        md.write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Original title
            status: needs_review
            ---
            Original body.
        """))
        syncer = _make_syncer(self.surreal, tmp_path)
        assert syncer.sync_file(md) is True
        assert _count_wiki_docs(self.surreal, "evolving") == 1

        # Mutate the file.
        md.write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Updated title
            status: auto_serve
            ---
            Updated body with new content.
        """))
        syncer2 = _make_syncer(self.surreal, tmp_path)
        # Must succeed (no UNIQUE violation on wiki_doc_path).
        assert syncer2.sync_file(md) is True

        # Still exactly ONE row (updated, not duplicated).
        assert _count_wiki_docs(self.surreal, "evolving") == 1

        doc = _get_wiki_doc(self.surreal, "evolving")
        assert doc is not None
        assert doc["title"] == "Updated title"
        assert doc["status"] == "auto_serve"


# ---------------------------------------------------------------------------
# Global-scope fresh-DB tests (ensure_schema coverage)
# ---------------------------------------------------------------------------

_GLOBAL_TEST_GROUP = "wiki-global-smoke-test"


def _make_global_client():
    """Return a live SurrealClient for the __global__-style test group_id."""
    from services.memory.surrealdb.client import SurrealClient, _schema_applied
    _schema_applied.discard(_GLOBAL_TEST_GROUP)
    return SurrealClient(
        group_id=_GLOBAL_TEST_GROUP,
        url=_SURREAL_URL,
        user=os.environ.get("SURREAL_USER", "root"),
        password=_SURREAL_PASS,
    )


def _apply_schema_for_group(surreal, group_id: str):
    from services.memory.ontology.registry import OntologyRegistry
    from services.memory.surrealdb.schema_gen import generate_schema_ddl, populate_ontology_meta
    import tempfile as _tmp
    registry = OntologyRegistry()
    composed = registry.compose(group_id)
    ddl = generate_schema_ddl(composed)
    with _tmp.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
        f.write(ddl)
        tmp = f.name
    try:
        surreal.apply_schema(tmp)
    finally:
        os.unlink(tmp)
    populate_ontology_meta(surreal, composed)


def _cleanup_group(surreal):
    for tbl in ("wiki_references", "wiki_mentions", "wiki_doc", "edge_staging"):
        try:
            surreal.query(f"DELETE {tbl}")
        except Exception:
            pass


@skip_if_no_surreal
class TestLiveGlobalScopeSync:
    """Live tests for global-scope (no prior schema) sync via sync_all_vignobles.

    Validates that ensure_schema is called before syncing so wiki_doc exists,
    and that INSTRUCTIONS.md / README.md are reserved (not ingested).
    """

    def setup_method(self):
        self.surreal = _make_global_client()
        # Explicitly do NOT apply schema here — the fix must apply it.
        _cleanup_group(self.surreal)

    def teardown_method(self):
        _cleanup_group(self.surreal)
        self.surreal.close()

    def test_fresh_db_sync_succeeds_with_ensure_schema(self, tmp_path, monkeypatch):
        """sync_all_vignobles into a fresh __global__-style DB (no prior schema) must not crash.

        Directly reproduces the original bug: sync_all_vignobles must call ensure_schema
        before syncing so that wiki_doc exists. If the fix is reverted this test crashes
        with 'The table wiki_doc does not exist'.

        GLOBAL_WIKI_GROUP is monkeypatched to the throwaway test scope so the test
        never touches the real production __global__ database.
        """
        import services.memory.wiki.sync_in as sync_in_mod
        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.wiki.sync_in import sync_all_vignobles
        from services.memory.surrealdb.client import _schema_applied

        # Redirect sync_all_vignobles to the throwaway test scope.
        monkeypatch.setattr(sync_in_mod, "GLOBAL_WIKI_GROUP", _GLOBAL_TEST_GROUP)

        # Ensure the schema cache does not mask a missing ensure_schema call.
        _schema_applied.discard(_GLOBAL_TEST_GROUP)

        # Build a minimal global wiki root with one OKF concept page.
        global_wiki = tmp_path / "global-wiki"
        global_wiki.mkdir()
        (global_wiki / "decision.md").write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Fresh DB decision
            status: auto_serve
            ---
            Body for fresh DB test.
        """))
        # Reserved files should be present but not ingested (dual coverage).
        (global_wiki / "INSTRUCTIONS.md").write_text("# Instructions\nStructural.\n")
        (global_wiki / "README.md").write_text("# Readme\nStructural.\n")

        # Use a nonexistent vignobles_base_dir so only the __global__ path runs.
        registry = OntologyRegistry()
        results = sync_all_vignobles(
            vignobles_base_dir=tmp_path / "no-vignobles",
            global_wiki_root=global_wiki,
            embed_fn=_noop_embed,
            registry=registry,
        )

        global_counts = results.get(_GLOBAL_TEST_GROUP, {}).get(_GLOBAL_TEST_GROUP, {})
        assert global_counts.get("errors", 1) == 0, (
            f"sync_all_vignobles crashed on fresh DB (no ensure_schema?): {global_counts}\nfull results: {results}"
        )
        assert global_counts.get("ingested", 0) == 1, (
            f"Expected 1 ingested concept page, got: {global_counts}"
        )

        # Verify the wiki_doc exists in the throwaway test scope (same client as setup_method).
        doc = _get_wiki_doc(self.surreal, "decision")
        assert doc is not None, "wiki_doc not found after sync_all_vignobles"
        assert doc["title"] == "Fresh DB decision"
        # Reserved files must not have been ingested.
        assert _get_wiki_doc(self.surreal, "INSTRUCTIONS") is None
        assert _get_wiki_doc(self.surreal, "README") is None

    def test_reserved_files_not_ingested(self, tmp_path):
        """INSTRUCTIONS.md and README.md must be skipped (not ingested as wiki_doc concepts)."""
        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.wiki.sync_in import WikiSyncer

        registry = OntologyRegistry()
        self.surreal.ensure_schema(registry=registry, group_id=_GLOBAL_TEST_GROUP)

        (tmp_path / "INSTRUCTIONS.md").write_text("# Instructions\nStructural file.\n")
        (tmp_path / "README.md").write_text("# Readme\nStructural file.\n")
        (tmp_path / "concept.md").write_text(textwrap.dedent("""\
            ---
            type: decision
            title: A real concept
            status: auto_serve
            ---
            This is a real OKF concept page.
        """))

        composed = registry.compose(_GLOBAL_TEST_GROUP)
        syncer = WikiSyncer(
            group_id=_GLOBAL_TEST_GROUP,
            surreal=self.surreal,
            embed_fn=_noop_embed,
            composed=composed,
            repo_path=tmp_path,
        )
        counts = syncer.sync_all()

        assert counts["errors"] == 0, f"Expected 0 errors, got: {counts}"
        assert counts["ingested"] == 1, f"Only the concept page should be ingested: {counts}"

        # Confirm neither reserved file was stored.
        assert _get_wiki_doc(self.surreal, "INSTRUCTIONS") is None
        assert _get_wiki_doc(self.surreal, "README") is None
        assert _get_wiki_doc(self.surreal, "concept") is not None

    def test_idempotency_global_scope(self, tmp_path):
        """Second run with no changes: 0 errors, 0 ingested, all skipped."""
        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.wiki.sync_in import WikiSyncer

        registry = OntologyRegistry()
        self.surreal.ensure_schema(registry=registry, group_id=_GLOBAL_TEST_GROUP)

        md = tmp_path / "stable.md"
        md.write_text(textwrap.dedent("""\
            ---
            type: decision
            title: Stable global page
            ---
            Unchanged content.
        """))

        composed = registry.compose(_GLOBAL_TEST_GROUP)

        syncer1 = WikiSyncer(
            group_id=_GLOBAL_TEST_GROUP,
            surreal=self.surreal,
            embed_fn=_noop_embed,
            composed=composed,
            repo_path=tmp_path,
        )
        counts1 = syncer1.sync_all()
        assert counts1["ingested"] == 1
        assert counts1["errors"] == 0

        # Second run — identical files.
        syncer2 = WikiSyncer(
            group_id=_GLOBAL_TEST_GROUP,
            surreal=self.surreal,
            embed_fn=_noop_embed,
            composed=composed,
            repo_path=tmp_path,
        )
        counts2 = syncer2.sync_all()
        assert counts2["errors"] == 0, f"Expected 0 errors on second run, got: {counts2}"
        assert counts2["ingested"] == 0
        assert counts2["skipped"] == 1
