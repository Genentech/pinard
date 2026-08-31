"""Inbound wiki git-sync: markdown → SurrealDB re-ingest.

Pulls the wiki git repository, diffs changed OKF markdown files, and upserts
them into the SurrealDB wiki_doc table with embeddings and typed link edges.

Idempotent via content_hash: unchanged files are skipped.
Loop-safe: never writes back to git (inbound only).

Reserved files (index.md, log.md) are ignored as concepts.

Usage::

    syncer = WikiSyncer(
        group_id="genomics-build",
        surreal=surreal_client,
        embed_fn=embed,
        composed=registry.compose("genomics-build"),
        repo_path=Path("/wiki-repos/genomics-build.wiki"),
    )
    syncer.pull()
    result = syncer.sync_all()
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger("pinard.memory.wiki.sync_in")

_RESERVED_NAMES = {"index.md", "log.md", "INSTRUCTIONS.md", "README.md"}

# Regex that matches a markdown heading line (H1/H2/H3 only).
_HEADING_SPLIT_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
# Maximum characters per chunk before paragraph-splitting.
_MAX_CHUNK_CHARS = 2000

def chunk_body(title: str, body: str) -> list[dict[str, Any]]:
    """Module-level chunking function (same logic as WikiSyncer._chunk_body).

    Exposed for use by the ingester --rechunk backfill path which has no
    WikiSyncer instance.  Returns a list of dicts with keys:
    heading, text, embed_text, chunk_index.
    """
    chunks: list[dict[str, Any]] = []

    matches = list(_HEADING_SPLIT_RE.finditer(body))

    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append((title, body.strip()))
    else:
        preamble = body[: matches[0].start()].strip()
        if preamble:
            sections.append((title, preamble))
        for i, m in enumerate(matches):
            heading_text = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            section_text = body[start:end].strip()
            sections.append((heading_text, section_text))

    idx = 0
    for heading, text in sections:
        if not text:
            continue
        if len(text) <= _MAX_CHUNK_CHARS:
            sub_chunks = [text]
        else:
            paragraphs = re.split(r"\n{2,}", text)
            sub_chunks = []
            current = ""
            for para in paragraphs:
                if not para.strip():
                    continue
                if current and len(current) + len(para) + 2 > _MAX_CHUNK_CHARS:
                    sub_chunks.append(current.strip())
                    current = para
                else:
                    current = (current + "\n\n" + para).strip() if current else para
            if current:
                sub_chunks.append(current.strip())

        for sub in sub_chunks:
            if not sub:
                continue
            embed_text = f"{title} — {heading}\n\n{sub}"
            chunks.append({
                "heading": heading,
                "text": sub,
                "embed_text": embed_text,
                "chunk_index": idx,
            })
            idx += 1

    return chunks



# Configurable global wiki group_id — override for testing to avoid writing to
# the real production __global__ scope.
GLOBAL_WIKI_GROUP: str = os.environ.get("GLOBAL_WIKI_GROUP", "__global__")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


class WikiSyncError(RuntimeError):
    pass


class WikiSyncer:
    """Sync OKF markdown pages from a git wiki repo into SurrealDB.

    Args:
        group_id: Tenant scope (matches the SurrealDB database / composed ontology).
        surreal: An open SurrealClient instance scoped to *group_id*.
        embed_fn: Callable(str) -> list[float]; embed a body string (e.g. Rosetta embed).
        composed: ComposedOntology for *group_id*; used to validate edge pairs and types.
        repo_path: Filesystem path to the cloned wiki git repository.
    """

    def __init__(
        self,
        group_id: str,
        surreal: Any,
        embed_fn: Callable[[str], list[float]],
        composed: Any,
        repo_path: Path,
    ) -> None:
        self.group_id = group_id
        self._surreal = surreal
        self._embed = embed_fn
        self._composed = composed
        self._repo_path = Path(repo_path)

        # Build lookup structures from composed ontology.
        # entity_roles: set of valid role strings (e.g. {"decision", "diagnosis", …})
        self._entity_roles: set[str] = set(composed.entity_roles())
        # edge_valid_pairs: {edge_name: set of (src_role, tgt_role)}
        self._edge_valid_pairs: dict[str, set[tuple[str, str]]] = {
            name: set(map(tuple, pairs))
            for name, pairs in composed.edge_type_map.items()
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def pull(self) -> None:
        """Run `git pull` on the wiki repo path."""
        try:
            result = subprocess.run(
                ["git", "pull"],
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug("git pull: %s", result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            raise WikiSyncError(f"git pull failed: {exc.stderr.strip()}") from exc

    def sync_all(self) -> dict[str, int]:
        """Walk all .md files in repo_path and sync changed ones.

        Returns a dict with counts: ingested, skipped, errors.
        """
        counts = {"ingested": 0, "skipped": 0, "errors": 0}
        for md_path in sorted(self._repo_path.rglob("*.md")):
            if md_path.name in _RESERVED_NAMES:
                continue
            try:
                ingested = self.sync_file(md_path)
                if ingested:
                    counts["ingested"] += 1
                else:
                    counts["skipped"] += 1
            except Exception:
                logger.exception("Error syncing %s", md_path)
                counts["errors"] += 1
        return counts

    def sync_file(self, md_path: Path) -> bool:
        """Sync a single markdown file into SurrealDB.

        Returns True if the file was ingested (new or changed), False if skipped.

        Idempotency: content_hash is computed over the frontmatter (with the
        ``content_hash`` key removed and keys sorted) plus the body — not over
        the raw file.  This makes the hash a fixpoint: emitting a page, reading
        it back, and emitting again produces the same hash, so unchanged pages
        are skipped and no spurious MRs are generated.
        """
        raw = md_path.read_text(encoding="utf-8")

        # Parse frontmatter + body first so we can compute a stable hash that
        # excludes the content_hash field itself (which would be self-referential).
        fm, body = self._parse_okf(raw)

        # Build a stable hash input: frontmatter without content_hash (key-sorted)
        # concatenated with the body.  This is a fixpoint across DB→disk round-trips.
        stable_fm = {k: v for k, v in fm.items() if k != "content_hash"}
        stable_input = json.dumps(stable_fm, sort_keys=True, ensure_ascii=False) + body
        content_hash = "sha256:" + hashlib.sha256(stable_input.encode()).hexdigest()

        # Derive the OKF path (concept ID) from the file's position in the repo.
        try:
            rel = md_path.relative_to(self._repo_path)
        except ValueError:
            rel = md_path
        okf_path = str(rel.with_suffix(""))

        # Derive a deterministic record id from path so that re-syncing an
        # updated file always upserts the same record (no duplicates, no
        # unique-index violations on wiki_doc_path).  Keep rid bare (no table
        # prefix) — _upsert_wiki_reference / _upsert_wiki_mention embed it in
        # type::record() server-side (bound string with colon is not a record id).
        rid = hashlib.sha256(f"wiki_doc\x00{okf_path}".encode()).hexdigest()[:32]

        # Check existing record by deterministic id — skip if hash matches (idempotent).
        existing = self._surreal.query(
            "SELECT content_hash FROM type::record('wiki_doc', $rid) LIMIT 1",
            {"rid": rid},
        )
        existing_hash = None
        if existing and existing[0]:
            row = existing[0]
            row = row[0] if isinstance(row, list) else row
            if isinstance(row, dict):
                existing_hash = row.get("content_hash")
        if existing_hash == content_hash:
            return False

        type_str = fm.get("type", "")
        title = fm.get("title", okf_path)
        summary = fm.get("summary", "")
        tags = fm.get("tags") or []
        confidence = float(fm.get("confidence", 1.0))
        frontmatter_status = fm.get("status", "")
        relations = fm.get("relations") or []

        status = self._resolve_status(type_str, confidence, frontmatter_status)

        # Embed the body.
        embedding = self._embed(body) if body.strip() else None

        # Build frontmatter object (preserve all keys except content_hash —
        # that is stored as a top-level wiki_doc column, not in frontmatter).
        fm_stored = {k: v for k, v in fm.items() if k != "content_hash"}

        wiki_doc_rid = rid

        # Upsert wiki_doc by deterministic id (path-keyed).
        self._surreal.query(
            "UPSERT type::record('wiki_doc', $rid) SET "
            "title = $title, type = $type, summary = $summary, body = $body, "
            "frontmatter = $frontmatter, path = $path, "
            "content_hash = $content_hash, "
            "confidence = $confidence, status = $status, "
            "embedding = $embedding, updated_at = time::now()",
            {
                "rid": rid,
                "title": title,
                "type": type_str,
                "summary": summary,
                "body": body,
                "frontmatter": fm_stored,
                "path": okf_path,
                "content_hash": content_hash,
                "confidence": confidence,
                "status": status,
                "embedding": embedding,
            },
        )

        self._materialize_links(wiki_doc_rid, body, relations, type_str)

        # Chunk + embed the body for fine-grained semantic recall (Part A, #190).
        try:
            chunks = self._chunk_body(title, body)
            self._surreal.delete_wiki_chunks_by_path(okf_path)
            if chunks:
                embedded_chunks = []
                for chunk in chunks:
                    chunk_embedding = None
                    try:
                        chunk_embedding = self._embed(chunk["embed_text"])
                    except Exception:
                        logger.debug("Chunk embed failed for %s chunk %d", okf_path, chunk["chunk_index"])
                    embedded_chunks.append({
                        "parent_path": okf_path,
                        "heading": chunk["heading"],
                        "chunk_index": chunk["chunk_index"],
                        "text": chunk["text"],
                        "embedding": chunk_embedding,
                    })
                self._surreal.upsert_wiki_chunks(embedded_chunks)
        except Exception:
            logger.warning("Chunk upsert failed for %s (page still synced)", okf_path, exc_info=True)

        logger.info("Synced %s (status=%s)", okf_path, status)
        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _chunk_body(
        self, title: str, body: str
    ) -> list[dict[str, Any]]:
        """Split a wiki body into per-heading chunks for embedding.

        Delegates to the module-level chunk_body() function.
        Returns a list of dicts with keys: heading, text, embed_text, chunk_index.
        """
        return chunk_body(title, body)
    def _parse_okf(self, raw: str) -> tuple[dict[str, Any], str]:
        """Split YAML frontmatter and markdown body from raw file content.

        Returns (frontmatter_dict, body_string).
        """
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            return {}, raw

        fm_raw = m.group(1)
        body = raw[m.end():]

        if yaml is None:
            raise WikiSyncError("PyYAML is required for OKF frontmatter parsing")

        try:
            fm = yaml.safe_load(fm_raw) or {}
        except yaml.YAMLError as exc:
            logger.warning("YAML parse error in frontmatter: %s", exc)
            fm = {}

        return fm, body

    def _resolve_status(
        self,
        type_str: str,
        confidence: float,
        frontmatter_status: str,
    ) -> str:
        """Resolve the storage status for a wiki page.

        - Unknown/empty type → needs_review (open-world, never drop).
        - Known type + frontmatter status → honor it.
        - Known type + no frontmatter status → needs_review (conservative default).
        """
        if not type_str or type_str not in self._entity_roles:
            return "needs_review"
        if frontmatter_status:
            return frontmatter_status
        return "needs_review"

    def _materialize_links(
        self,
        src_rid: str,
        body: str,
        relations: list[dict[str, Any]],
        src_type: str,
    ) -> None:
        """Materialize typed link edges from body markdown links and relations list.

        src_rid is the bare 32-char hex rid of the source wiki_doc (no table prefix).

        - Markdown [text](path) → wiki_references (wiki↔wiki) with no edge_type.
        - relations: list → wiki_references or wiki_mentions with validated edge_type.
        - Invalid (src_role, tgt_role) pairs → logged + soft-staged; page kept.
        - Unknown targets → logged and skipped.
        """
        # 1. Markdown body links → wiki_references (untyped; plain cross-links).
        for m in _MD_LINK_RE.finditer(body):
            target_path = m.group(2)
            # Skip external URLs.
            if target_path.startswith("http://") or target_path.startswith("https://"):
                continue
            # Strip fragment (#section) if present.
            target_path = target_path.split("#")[0].strip()
            if not target_path:
                continue
            self._upsert_wiki_reference(src_rid, target_path, edge_type="")

        # 2. Explicit relations: frontmatter `relations:` list.
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            edge_name = rel.get("edge", "")
            target_path = rel.get("to", "")
            if not edge_name or not target_path:
                logger.warning("Malformed relation entry (missing edge or to): %s", rel)
                continue

            # Validate edge pair against ontology.
            valid = self._validate_edge(edge_name, src_type, target_path)
            if not valid:
                # Soft-stage the bad edge; keep the page.
                self._stage_bad_edge(src_rid, edge_name, src_type, target_path)
                continue

            # Check whether target is a wiki_doc (by deterministic id) or entity.
            # SELECT from the deterministic id to confirm the target wiki_doc exists.
            tgt_wiki_rid = self._wiki_doc_rid(target_path)
            tgt_wiki = self._surreal.query(
                "SELECT id FROM type::record('wiki_doc', $rid) LIMIT 1",
                {"rid": tgt_wiki_rid},
            )
            if tgt_wiki and tgt_wiki[0]:
                row = tgt_wiki[0]
                row = row[0] if isinstance(row, list) else row
                if isinstance(row, dict) and row.get("id"):
                    self._upsert_wiki_reference(src_rid, target_path, edge_type=edge_name)
                    continue

            # Try to find a matching entity by name; extract its bare rid.
            tgt_entity = self._surreal.query(
                "SELECT id FROM entity WHERE name = $name LIMIT 1",
                {"name": target_path},
            )
            if tgt_entity and tgt_entity[0]:
                row = tgt_entity[0]
                row = row[0] if isinstance(row, list) else row
                if isinstance(row, dict) and row.get("id"):
                    entity_id_str = str(row["id"])  # e.g. "entity:abc123"
                    entity_rid = entity_id_str.split(":", 1)[-1]
                    self._upsert_wiki_mention(src_rid, entity_rid, edge_type=edge_name)
                    continue

            logger.warning(
                "Relation target not found (neither wiki_doc nor entity): %s", target_path
            )

    def _validate_edge(self, edge_name: str, src_type: str, target_path: str) -> bool:
        """Return True if edge_name is valid for src_type per the ontology.

        Unknown edge names and missing src_type roles are treated as invalid.
        """
        valid_pairs = self._edge_valid_pairs.get(edge_name)
        if valid_pairs is None:
            logger.warning("Unknown edge type %r — staging edge", edge_name)
            return False
        if not src_type or src_type not in self._entity_roles:
            logger.warning(
                "Cannot validate edge %r: src_type %r not in ontology", edge_name, src_type
            )
            return False
        # Check that at least one valid pair starts with src_type.
        # (We don't know the target type at validation time — we allow if src_type matches.)
        matching = [p for p in valid_pairs if p[0] == src_type]
        if not matching:
            logger.warning(
                "Edge %r not valid for src_type %r — staging edge", edge_name, src_type
            )
            return False
        return True

    def _wiki_doc_rid(self, path: str) -> str:
        """Return the bare 32-char hex rid for a wiki_doc path (no table prefix)."""
        return hashlib.sha256(f"wiki_doc\x00{path}".encode()).hexdigest()[:32]

    def _upsert_wiki_reference(
        self, src_rid: str, target_path: str, edge_type: str
    ) -> None:
        """Insert a wiki_references edge between two wiki_doc records.

        Uses type::record(...) inline in SurrealQL — bound string params are NOT
        interpreted as record ids by RELATE (the colon-string gotcha), so both
        endpoints must be constructed server-side via type::record().
        If the target page doesn't exist yet the RELATE fails silently (best-effort).
        """
        tgt_rid = self._wiki_doc_rid(target_path)
        try:
            self._surreal.query(
                "RELATE (type::record('wiki_doc', $src_rid))"
                "->wiki_references->"
                "(type::record('wiki_doc', $tgt_rid)) "
                "SET edge_type = $edge_type",
                {"src_rid": src_rid, "tgt_rid": tgt_rid, "edge_type": edge_type},
            )
        except Exception:
            logger.debug(
                "wiki_references edge not created (target may not exist yet): %s",
                target_path, exc_info=True,
            )

    def _upsert_wiki_mention(
        self, src_rid: str, tgt_entity_rid: str, edge_type: str
    ) -> None:
        """Insert a wiki_mentions edge from a wiki_doc to an entity.

        Uses type::record(...) inline in SurrealQL for both endpoints.
        """
        self._surreal.query(
            "RELATE (type::record('wiki_doc', $src_rid))"
            "->wiki_mentions->"
            "(type::record('entity', $tgt_rid)) "
            "SET edge_type = $edge_type",
            {"src_rid": src_rid, "tgt_rid": tgt_entity_rid, "edge_type": edge_type},
        )

    def _stage_bad_edge(
        self,
        src_rid: str,
        edge_name: str,
        src_type: str,
        target_path: str,
    ) -> None:
        """Soft-stage an invalid/unknown edge in edge_staging.

        Uses a deterministic record id keyed on (from_name, to_name, proposed_relation)
        so repeated staging of the same bad edge upserts (increments occurrence_count)
        rather than inserting duplicate rows.  edge_staging has no UNIQUE index, so
        INSERT … ON DUPLICATE KEY cannot fire — UPSERT by rid is the correct dedup.
        """
        from_name = src_rid
        rid = hashlib.sha256(
            f"edge_staging\x00{from_name}\x00{target_path}\x00{edge_name}".encode()
        ).hexdigest()[:32]
        try:
            self._surreal.query(
                "UPSERT type::record('edge_staging', $rid) SET "
                "from_name = $from_name, from_role = $from_role, "
                "to_name = $to_name, proposed_relation = $proposed_relation, "
                "provenance = $provenance, "
                "occurrence_count += 1, "
                "updated_at = time::now()",
                {
                    "rid": rid,
                    "from_name": from_name,
                    "from_role": src_type,
                    "to_name": target_path,
                    "proposed_relation": edge_name,
                    "provenance": "wiki_sync_in",
                },
            )
        except Exception:
            # edge_staging is best-effort; never fail the page ingest.
            logger.debug("Could not stage bad edge %r → %r", src_rid, target_path, exc_info=True)


# ── Multi-vignoble entry point ───────────────────────────────────────────────

def sync_all_vignobles(
    vignobles_base_dir: Path | str,
    global_wiki_root: Path | str | None,
    embed_fn: Any,
    registry: Any,
) -> dict[str, Any]:
    """Pull + re-ingest wiki pages for every vignoble found under *vignobles_base_dir*.

    For each subdirectory under *vignobles_base_dir*, reads ``vignes.yaml`` to
    discover group_ids and syncs ``<vignoble-dir>/wiki/`` into SurrealDB for each
    group_id.  If *global_wiki_root* is set, also syncs it into the ``__global__``
    SurrealDB scope.

    Best-effort: a failure for one vignoble or group_id does not abort the rest.

    Args:
        vignobles_base_dir: Parent directory of vignoble clones.
        global_wiki_root: Path to the global pinard-wiki clone (may be None).
        embed_fn: Embedding callable.
        registry: OntologyRegistry instance.

    Returns:
        Aggregated counts ``{vignoble_name: {group_id: {ingested, skipped, errors}}}``.
    """
    try:
        import yaml as _yaml  # type: ignore[import]
    except ImportError:
        _yaml = None  # type: ignore[assignment]

    from services.memory.surrealdb.client import SurrealClient  # local import to avoid circular

    base = Path(vignobles_base_dir)
    results: dict[str, Any] = {}

    if base.exists():
        for vignoble_dir in sorted(base.iterdir()):
            if not vignoble_dir.is_dir():
                continue
            vignoble_name = vignoble_dir.name

            vignes_yaml = vignoble_dir / "vignes.yaml"
            if not vignes_yaml.exists():
                logger.debug("No vignes.yaml in %s — skipping", vignoble_dir)
                continue

            if _yaml is None:
                logger.warning("PyYAML not available — cannot read %s", vignes_yaml)
                continue

            try:
                with open(vignes_yaml) as f:
                    vignes_data = _yaml.safe_load(f) or {}
            except Exception as exc:
                logger.warning("Failed to read %s: %s — skipping vignoble", vignes_yaml, exc)
                continue

            group_ids = list((vignes_data.get("vignes") or {}).keys())
            wiki_dir = vignoble_dir / "wiki"

            results[vignoble_name] = {}

            for group_id in group_ids:
                group_wiki_dir = wiki_dir / group_id
                if not group_wiki_dir.exists():
                    logger.debug("wiki dir %s does not exist — skipping group %s", group_wiki_dir, group_id)
                    results[vignoble_name][group_id] = {"skipped": 0, "ingested": 0, "errors": 0}
                    continue
                try:
                    composed = registry.compose(group_id)
                    with SurrealClient(group_id=group_id) as surreal:
                        surreal.ensure_schema(registry=registry, group_id=group_id)
                        syncer = WikiSyncer(
                            group_id=group_id,
                            surreal=surreal,
                            embed_fn=embed_fn,
                            composed=composed,
                            repo_path=group_wiki_dir,
                        )
                        try:
                            syncer.pull()
                        except WikiSyncError as exc:
                            logger.warning("git pull failed for %s/%s: %s — continuing with existing files", vignoble_name, group_id, exc)
                        counts = syncer.sync_all()
                        results[vignoble_name][group_id] = counts
                        logger.info(
                            "Synced vignoble=%s group=%s: %s",
                            vignoble_name, group_id, counts,
                        )
                except Exception:
                    logger.exception(
                        "Error syncing vignoble=%s group=%s — continuing",
                        vignoble_name, group_id,
                    )
                    results[vignoble_name][group_id] = {"errors": 1}
    else:
        logger.warning(
            "sync_all_vignobles: vignobles base dir %s does not exist — skipping vignoble sync",
            base,
        )

    if global_wiki_root:
        global_path = Path(global_wiki_root)
        global_group_id = GLOBAL_WIKI_GROUP
        if global_path.exists():
            try:
                composed = registry.compose(global_group_id)
                with SurrealClient(group_id=global_group_id) as surreal:
                    surreal.ensure_schema(registry=registry, group_id=global_group_id)
                    syncer = WikiSyncer(
                        group_id=global_group_id,
                        surreal=surreal,
                        embed_fn=embed_fn,
                        composed=composed,
                        repo_path=global_path,
                    )
                    try:
                        syncer.pull()
                    except WikiSyncError as exc:
                        logger.warning("git pull failed for global wiki: %s — continuing", exc)
                    counts = syncer.sync_all()
                    results[global_group_id] = {global_group_id: counts}
                    logger.info("Synced global wiki: %s", counts)
            except Exception:
                logger.exception("Error syncing global wiki — continuing")
                results[global_group_id] = {global_group_id: {"errors": 1}}
        else:
            logger.debug("global_wiki_root %s does not exist — skipping global sync", global_path)

    return results
