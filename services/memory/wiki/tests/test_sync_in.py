"""Unit tests for services/memory/wiki/sync_in.py.

No LLM, no real SurrealDB, no network — all external calls are mocked.

Tests:
- Valid OKF page → wiki_doc upserted + typed wiki_references edge.
- Unknown type → page ingested as needs_review.
- Invalid edge pair → page kept, edge not materialized (staged instead).
- Re-running sync with unchanged file → no-op (idempotent).
- Reserved files (index.md, log.md) → skipped.
- Markdown body links → wiki_references with no edge_type.
"""
from __future__ import annotations

import hashlib
import json
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from services.memory.wiki.sync_in import WikiSyncer, WikiSyncError
from services.memory.ontology.registry import OntologyRegistry
from services.memory.ontology.entities import CoreEntity
from services.memory.ontology.edges import CoreEdge
from pydantic import Field


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

VALID_PAGE = """\
---
type: decision
title: SurrealDB pivot
description: We chose SurrealDB.
tags: [storage]
confidence: 0.9
status: auto_serve
relations:
  - edge: ResolvedBy
    to: actions/rewrite-client
---

# SurrealDB pivot

We chose SurrealDB for the memory layer.
"""

UNKNOWN_TYPE_PAGE = """\
---
type: novel_concept
title: Something new
---

Body text here.
"""

NO_FRONTMATTER_PAGE = """\
# Just markdown

No frontmatter at all.
"""

PAGE_WITH_MD_LINK = """\
---
type: decision
title: Link test
---

See [the action](actions/some-action) for details.
"""


def _make_surreal(existing_hash: str | None = None) -> MagicMock:
    """Create a mock SurrealClient."""
    surreal = MagicMock()

    # Default: no existing record (empty result).
    def _query(sql: str, params: dict | None = None):
        sql_strip = sql.strip()
        if "SELECT content_hash FROM type::record" in sql_strip:
            if existing_hash is not None:
                return [[{"content_hash": existing_hash}]]
            return [[]]
        if "SELECT id FROM type::record" in sql_strip:
            # Target wiki_doc exists by default.
            return [[{"id": "wiki_doc:abc"}]]
        if "SELECT id FROM entity" in sql_strip:
            return [[]]
        return [[]]

    surreal.query = MagicMock(side_effect=_query)
    return surreal


def _make_composed(
    entity_roles: list[str] | None = None,
    edge_type_map: dict | None = None,
) -> MagicMock:
    composed = MagicMock()
    composed.entity_roles.return_value = entity_roles or [
        "decision", "diagnosis", "action", "artifact",
        "step", "task", "verdict", "gate", "log_pattern", "environment_condition",
    ]
    composed.edge_type_map = edge_type_map or {
        "ResolvedBy": [("decision", "action"), ("diagnosis", "action")],
        "DependsOn": [("task", "task")],
        "Produces": [("action", "artifact")],
    }
    return composed


def _make_embed() -> MagicMock:
    embed = MagicMock(return_value=[0.1] * 1024)
    return embed


def _make_syncer(
    tmp_path: Path,
    existing_hash: str | None = None,
    entity_roles: list[str] | None = None,
    edge_type_map: dict | None = None,
) -> tuple[WikiSyncer, MagicMock]:
    surreal = _make_surreal(existing_hash=existing_hash)
    composed = _make_composed(entity_roles=entity_roles, edge_type_map=edge_type_map)
    embed = _make_embed()
    syncer = WikiSyncer(
        group_id="test",
        surreal=surreal,
        embed_fn=embed,
        composed=composed,
        repo_path=tmp_path,
    )
    return syncer, surreal


def _stable_content_hash(raw: str, syncer: WikiSyncer) -> str:
    """Compute content_hash the same way sync_file() does (stable, excludes content_hash key)."""
    fm, body = syncer._parse_okf(raw)
    stable_fm = {k: v for k, v in fm.items() if k != "content_hash"}
    stable_input = json.dumps(stable_fm, sort_keys=True, ensure_ascii=False) + body
    return "sha256:" + hashlib.sha256(stable_input.encode()).hexdigest()


# ---------------------------------------------------------------------------
# _parse_okf
# ---------------------------------------------------------------------------

class TestParseOkf:
    def test_parses_frontmatter_and_body(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        fm, body = syncer._parse_okf(VALID_PAGE)
        assert fm["type"] == "decision"
        assert fm["title"] == "SurrealDB pivot"
        assert fm["confidence"] == 0.9
        assert "SurrealDB pivot" in body

    def test_no_frontmatter_returns_empty_dict(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        fm, body = syncer._parse_okf(NO_FRONTMATTER_PAGE)
        assert fm == {}
        assert "Just markdown" in body

    def test_relations_parsed(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        fm, _ = syncer._parse_okf(VALID_PAGE)
        assert fm["relations"] == [{"edge": "ResolvedBy", "to": "actions/rewrite-client"}]


# ---------------------------------------------------------------------------
# _resolve_status
# ---------------------------------------------------------------------------

class TestResolveStatus:
    def test_known_type_with_status(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        assert syncer._resolve_status("decision", 0.9, "auto_serve") == "auto_serve"

    def test_known_type_no_status(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        assert syncer._resolve_status("decision", 0.9, "") == "needs_review"

    def test_unknown_type_always_needs_review(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        assert syncer._resolve_status("novel_concept", 0.99, "auto_serve") == "needs_review"

    def test_empty_type_needs_review(self, tmp_path):
        syncer, _ = _make_syncer(tmp_path)
        assert syncer._resolve_status("", 1.0, "auto_serve") == "needs_review"


# ---------------------------------------------------------------------------
# sync_file — valid page
# ---------------------------------------------------------------------------

class TestSyncFileValid:
    def test_upserts_wiki_doc(self, tmp_path):
        md = tmp_path / "decisions" / "surrealdb-pivot.md"
        md.parent.mkdir()
        md.write_text(VALID_PAGE)

        syncer, surreal = _make_syncer(tmp_path)
        result = syncer.sync_file(md)

        assert result is True
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 1
        params = upsert_calls[0][0][1]
        assert params["type"] == "decision"
        assert params["title"] == "SurrealDB pivot"
        assert params["status"] == "auto_serve"
        assert params["path"] == "decisions/surrealdb-pivot"
        # Deterministic id must be present (path-keyed, not title-keyed)
        assert "rid" in params
        assert len(params["rid"]) == 32  # 32-char hex sha256 prefix

    def test_creates_wiki_reference_for_relation(self, tmp_path):
        md = tmp_path / "decisions" / "surrealdb-pivot.md"
        md.parent.mkdir()
        md.write_text(VALID_PAGE)

        surreal = MagicMock()

        def _query(sql: str, params: dict | None = None):
            if "SELECT content_hash FROM type::record" in sql:
                return [[]]
            if "SELECT id FROM type::record" in sql:
                return [[{"id": "wiki_doc:abc"}]]
            if "SELECT id FROM entity" in sql:
                return [[]]
            return [[]]

        surreal.query = MagicMock(side_effect=_query)
        composed = _make_composed()
        syncer = WikiSyncer(
            group_id="test",
            surreal=surreal,
            embed_fn=_make_embed(),
            composed=composed,
            repo_path=tmp_path,
        )
        syncer.sync_file(md)

        relate_calls = [
            c for c in surreal.query.call_args_list
            if "RELATE" in c[0][0] and "wiki_references" in c[0][0]
        ]
        assert len(relate_calls) >= 1
        # The ResolvedBy relation should carry the edge_type.
        edge_types_set = {c[0][1].get("edge_type") for c in relate_calls}
        assert "ResolvedBy" in edge_types_set

    def test_embeds_body(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(VALID_PAGE)
        embed = _make_embed()
        surreal = _make_surreal()
        composed = _make_composed()
        syncer = WikiSyncer("test", surreal, embed, composed, tmp_path)
        syncer.sync_file(md)
        # embed is now called once for the whole-page body + once per chunk.
        assert embed.call_count >= 1


# ---------------------------------------------------------------------------
# sync_file — unknown type
# ---------------------------------------------------------------------------

class TestSyncFileUnknownType:
    def test_ingested_as_needs_review(self, tmp_path):
        md = tmp_path / "novel.md"
        md.write_text(UNKNOWN_TYPE_PAGE)
        syncer, surreal = _make_syncer(tmp_path)
        result = syncer.sync_file(md)
        assert result is True
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 1
        assert upsert_calls[0][0][1]["status"] == "needs_review"

    def test_not_dropped(self, tmp_path):
        md = tmp_path / "novel.md"
        md.write_text(UNKNOWN_TYPE_PAGE)
        syncer, surreal = _make_syncer(tmp_path)
        result = syncer.sync_file(md)
        assert result is True


# ---------------------------------------------------------------------------
# sync_file — invalid edge pair
# ---------------------------------------------------------------------------

class TestSyncFileInvalidEdge:
    def test_page_kept_edge_not_materialized(self, tmp_path):
        # decision → DependsOn → task is NOT in valid_pairs for decision src.
        # valid_pairs for DependsOn: [("task", "task")] — decision is not allowed.
        page = """\
---
type: decision
title: Bad edge test
relations:
  - edge: DependsOn
    to: actions/something
---

Body.
"""
        md = tmp_path / "bad-edge.md"
        md.write_text(page)

        surreal = _make_surreal()
        composed = _make_composed()
        embed = _make_embed()
        syncer = WikiSyncer("test", surreal, embed, composed, tmp_path)
        result = syncer.sync_file(md)

        # Page must be ingested.
        assert result is True
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 1

        # RELATE must NOT be called for the invalid edge.
        relate_calls = [
            c for c in surreal.query.call_args_list
            if "RELATE" in c[0][0] and "wiki_references" in c[0][0]
        ]
        assert len(relate_calls) == 0

    def test_unknown_edge_name_staged_not_materialized(self, tmp_path):
        page = """\
---
type: decision
title: Unknown edge test
relations:
  - edge: DoesNotExist
    to: some/page
---

Body.
"""
        md = tmp_path / "unknown-edge.md"
        md.write_text(page)
        syncer, surreal = _make_syncer(tmp_path)
        result = syncer.sync_file(md)
        assert result is True

        relate_calls = [
            c for c in surreal.query.call_args_list
            if "RELATE" in c[0][0]
        ]
        assert len(relate_calls) == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_unchanged_file_skipped(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(VALID_PAGE)
        # Compute hash the stable way (excluding content_hash key) so it matches
        # what sync_file() will compute — this ensures idempotency.
        syncer_ref, _ = _make_syncer(tmp_path)
        content_hash = _stable_content_hash(VALID_PAGE, syncer_ref)

        syncer, surreal = _make_syncer(tmp_path, existing_hash=content_hash)
        result = syncer.sync_file(md)

        assert result is False
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 0

    def test_changed_file_reingested(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(VALID_PAGE)
        # Provide a different hash — file has changed.
        syncer, surreal = _make_syncer(tmp_path, existing_hash="sha256:oldhash")
        result = syncer.sync_file(md)
        assert result is True

    def test_same_rid_for_same_path(self, tmp_path):
        """Two sync_file calls for the same path produce the same rid (upsert, not insert)."""
        import hashlib
        md = tmp_path / "decisions" / "pivot.md"
        md.parent.mkdir()
        md.write_text(VALID_PAGE)

        rids = []
        for _ in range(2):
            syncer, surreal = _make_syncer(tmp_path)
            # Intercept the upsert call to capture rid.
            syncer.sync_file(md)
            upsert_calls = [
                c for c in surreal.query.call_args_list
                if "UPSERT type::record('wiki_doc'" in c[0][0]
            ]
            if upsert_calls:
                rids.append(upsert_calls[0][0][1]["rid"])

        assert len(rids) == 2
        assert rids[0] == rids[1], "rid must be stable (path-keyed) across re-syncs"

    def test_content_hash_is_stable_across_roundtrip(self, tmp_path):
        """content_hash must be a fixpoint across curator→disk→sync_in round-trips.

        The curator no longer writes content_hash into on-disk frontmatter — it is
        a DB-only idempotency column.  Verify that:
        1. sync_file() stores a stable hash in the DB (no content_hash key in input).
        2. The curator emits the page WITHOUT content_hash in the frontmatter.
        3. A second sync of that clean on-disk file (with the stored DB hash as the
           existing hash) is idempotent — sync_file() returns False.
        """
        # Step 1: sync a plain page (no content_hash in frontmatter).
        md = tmp_path / "decisions" / "roundtrip.md"
        md.parent.mkdir()
        md.write_text(VALID_PAGE)

        syncer, surreal = _make_syncer(tmp_path)
        result = syncer.sync_file(md)
        assert result is True

        # Capture the content_hash that was stored in the DB.
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 1
        stored_hash = upsert_calls[0][0][1]["content_hash"]

        # Verify it equals the stable hash (no content_hash key in input).
        expected_hash = _stable_content_hash(VALID_PAGE, syncer)
        assert stored_hash == expected_hash

        # Verify content_hash is NOT stored inside the frontmatter column.
        stored_frontmatter = upsert_calls[0][0][1]["frontmatter"]
        assert "content_hash" not in stored_frontmatter, (
            "content_hash must not appear in the frontmatter column — DB-only field"
        )

        # Step 2: simulate what the curator now emits — the page on disk has NO
        # content_hash line (curator pops it before writing).  The file is identical
        # to VALID_PAGE (no content_hash was ever written).
        # sync_file() must return False when the DB already holds the correct hash.
        syncer2, surreal2 = _make_syncer(tmp_path, existing_hash=stored_hash)
        result2 = syncer2.sync_file(md)
        assert result2 is False, (
            "Re-syncing the same clean on-disk file must be idempotent — "
            "DB content_hash already matches"
        )


# ---------------------------------------------------------------------------
# Reserved files
# ---------------------------------------------------------------------------

class TestReservedFiles:
    def test_index_md_skipped_in_sync_all(self, tmp_path):
        (tmp_path / "index.md").write_text("# Index\n")
        (tmp_path / "log.md").write_text("# Log\n")
        syncer, surreal = _make_syncer(tmp_path)
        counts = syncer.sync_all()
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 0

    def test_instructions_and_readme_skipped_in_sync_all(self, tmp_path):
        (tmp_path / "INSTRUCTIONS.md").write_text("# Instructions\n")
        (tmp_path / "README.md").write_text("# Readme\n")
        syncer, surreal = _make_syncer(tmp_path)
        counts = syncer.sync_all()
        upsert_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_calls) == 0
        assert counts["ingested"] == 0
        assert counts["errors"] == 0

    def test_all_reserved_names_skipped_together(self, tmp_path):
        for name in ("index.md", "log.md", "INSTRUCTIONS.md", "README.md"):
            (tmp_path / name).write_text(f"# {name}\n")
        (tmp_path / "decision.md").write_text(VALID_PAGE)
        syncer, surreal = _make_syncer(tmp_path)
        counts = syncer.sync_all()
        assert counts["ingested"] == 1
        assert counts["errors"] == 0

    def test_normal_md_processed(self, tmp_path):
        (tmp_path / "decision.md").write_text(VALID_PAGE)
        (tmp_path / "index.md").write_text("# Index\n")
        syncer, surreal = _make_syncer(tmp_path)
        counts = syncer.sync_all()
        assert counts["ingested"] == 1


# ---------------------------------------------------------------------------
# Markdown body links
# ---------------------------------------------------------------------------

class TestMarkdownBodyLinks:
    def test_md_link_creates_wiki_reference_no_edge_type(self, tmp_path):
        md = tmp_path / "link-test.md"
        md.write_text(PAGE_WITH_MD_LINK)

        surreal = MagicMock()

        def _query(sql: str, params: dict | None = None):
            if "SELECT content_hash FROM type::record" in sql:
                return [[]]
            if "SELECT id FROM type::record" in sql:
                return [[{"id": "wiki_doc:abc"}]]
            if "SELECT id FROM entity" in sql:
                return [[]]
            return [[]]

        surreal.query = MagicMock(side_effect=_query)
        composed = _make_composed()
        syncer = WikiSyncer("test", surreal, _make_embed(), composed, tmp_path)
        syncer.sync_file(md)

        relate_calls = [
            c for c in surreal.query.call_args_list
            if "RELATE" in c[0][0] and "wiki_references" in c[0][0]
        ]
        # At least the body link should be materialized.
        assert len(relate_calls) >= 1
        # Body links have empty edge_type.
        body_link_calls = [
            c for c in relate_calls if c[0][1].get("edge_type") == ""
        ]
        assert len(body_link_calls) >= 1

    def test_external_urls_not_followed(self, tmp_path):
        page = """\
---
type: decision
title: External link test
---

See [docs](https://example.com/docs) for details.
"""
        md = tmp_path / "external.md"
        md.write_text(page)
        syncer, surreal = _make_syncer(tmp_path)
        syncer.sync_file(md)

        relate_calls = [
            c for c in surreal.query.call_args_list
            if "RELATE" in c[0][0]
        ]
        assert len(relate_calls) == 0


# ---------------------------------------------------------------------------
# sync_all counts
# ---------------------------------------------------------------------------

class TestSyncAll:
    def test_returns_counts(self, tmp_path):
        (tmp_path / "a.md").write_text(VALID_PAGE)
        (tmp_path / "b.md").write_text(UNKNOWN_TYPE_PAGE)
        syncer, _ = _make_syncer(tmp_path)
        counts = syncer.sync_all()
        assert counts["ingested"] == 2
        assert counts["skipped"] == 0
        assert counts["errors"] == 0

    def test_skipped_count_on_unchanged(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(VALID_PAGE)
        # Compute hash the stable way to match what sync_file() will compute.
        syncer_ref, _ = _make_syncer(tmp_path)
        content_hash = _stable_content_hash(VALID_PAGE, syncer_ref)
        syncer, _ = _make_syncer(tmp_path, existing_hash=content_hash)
        counts = syncer.sync_all()
        assert counts["skipped"] == 1
        assert counts["ingested"] == 0


# ---------------------------------------------------------------------------
# chunk_body — module-level chunking function
# ---------------------------------------------------------------------------

class TestChunkBody:
    def test_no_headings_single_chunk(self):
        from services.memory.wiki.sync_in import chunk_body
        chunks = chunk_body("My Page", "Some body text without any headings.")
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "My Page"
        assert "Some body text" in chunks[0]["text"]
        assert chunks[0]["chunk_index"] == 0

    def test_splits_on_headings(self):
        from services.memory.wiki.sync_in import chunk_body
        body = "## Section A\n\nContent A\n\n## Section B\n\nContent B"
        chunks = chunk_body("Title", body)
        assert len(chunks) == 2
        assert chunks[0]["heading"] == "Section A"
        assert chunks[1]["heading"] == "Section B"
        assert chunks[0]["chunk_index"] == 0
        assert chunks[1]["chunk_index"] == 1

    def test_preamble_kept(self):
        from services.memory.wiki.sync_in import chunk_body
        body = "Intro text\n\n## Section A\n\nContent A"
        chunks = chunk_body("Title", body)
        assert len(chunks) == 2
        assert chunks[0]["heading"] == "Title"
        assert "Intro text" in chunks[0]["text"]

    def test_embed_text_prefixed(self):
        from services.memory.wiki.sync_in import chunk_body
        body = "## My Section\n\nSection content here."
        chunks = chunk_body("Page Title", body)
        assert len(chunks) == 1
        assert chunks[0]["embed_text"].startswith("Page Title \u2014 My Section")
        assert "Section content here" in chunks[0]["embed_text"]

    def test_oversized_section_sub_splits(self):
        from services.memory.wiki.sync_in import chunk_body, _MAX_CHUNK_CHARS
        para = "A" * 300
        # Build a section that exceeds _MAX_CHUNK_CHARS
        body = "## Big Section\n\n" + "\n\n".join([para] * 10)
        chunks = chunk_body("Title", body)
        # Should have been split into multiple chunks
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk["text"]) <= _MAX_CHUNK_CHARS + 300  # paragraph granularity

    def test_empty_body_no_chunks(self):
        from services.memory.wiki.sync_in import chunk_body
        chunks = chunk_body("Title", "")
        assert chunks == []

    def test_chunk_indices_sequential(self):
        from services.memory.wiki.sync_in import chunk_body
        body = "## A\n\nContent A\n\n## B\n\nContent B\n\n## C\n\nContent C"
        chunks = chunk_body("Title", body)
        indices = [c["chunk_index"] for c in chunks]
        assert indices == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# WikiSyncer — chunk upsert integration
# ---------------------------------------------------------------------------

class TestSyncFileChunks:
    def test_chunks_upserted_on_sync(self, tmp_path):
        """sync_file calls delete_wiki_chunks_by_path + upsert_wiki_chunks."""
        md = tmp_path / "chunked.md"
        md.write_text(VALID_PAGE)
        embed = _make_embed()
        surreal = _make_surreal()
        composed = _make_composed()
        syncer = WikiSyncer("test", surreal, embed, composed, tmp_path)
        syncer.sync_file(md)

        delete_calls = [
            c for c in surreal.delete_wiki_chunks_by_path.call_args_list
        ]
        assert len(delete_calls) == 1

        upsert_calls = [
            c for c in surreal.upsert_wiki_chunks.call_args_list
        ]
        assert len(upsert_calls) == 1
        chunks_arg = upsert_calls[0][0][0]
        assert isinstance(chunks_arg, list)
        assert len(chunks_arg) >= 1
        for chunk in chunks_arg:
            assert "parent_path" in chunk
            assert "heading" in chunk
            assert "chunk_index" in chunk
            assert "text" in chunk

    def test_chunk_failure_does_not_block_page(self, tmp_path):
        """If chunk upsert raises, the page is still considered synced."""
        md = tmp_path / "chunked.md"
        md.write_text(VALID_PAGE)
        embed = _make_embed()
        surreal = _make_surreal()
        surreal.upsert_wiki_chunks.side_effect = RuntimeError("DB error")
        composed = _make_composed()
        syncer = WikiSyncer("test", surreal, embed, composed, tmp_path)
        result = syncer.sync_file(md)
        # Page still synced despite chunk failure
        assert result is True
        upsert_wiki_calls = [
            c for c in surreal.query.call_args_list
            if "UPSERT type::record('wiki_doc'" in c[0][0]
        ]
        assert len(upsert_wiki_calls) == 1
