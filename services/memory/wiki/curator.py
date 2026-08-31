"""Outbound wiki curator: SurrealDB typed graph → OKF markdown pages.

Reads the ontology-typed SurrealDB graph for a group_id, clusters related
entities/edges into concept candidates, synthesizes OKF markdown pages via
build_llm_client(), and commits them to the wiki git repo as a branch + MR.

Incremental via wiki_curator_cursor: only (re)synthesizes concepts whose
underlying entities changed since the last cursor position. Deduplicates via
cosine similarity scan against existing wiki_doc embeddings. Human-authored
pages (frontmatter.source == "human") are never overwritten.

Usage::

    from services.memory.wiki.curator import WikiCurator
    from services.memory.llm_client import build_llm_client
    from services.memory.embeddings import embed
    from services.memory.surrealdb.client import SurrealClient
    from services.memory.ontology.registry import OntologyRegistry

    registry = OntologyRegistry()
    composed = registry.compose("genomics-build")
    surreal = SurrealClient(group_id="genomics-build")
    llm = build_llm_client()

    curator = WikiCurator(
        group_id="genomics-build",
        surreal=surreal,
        embed_fn=embed,
        composed=composed,
        repo_path=Path("/wiki-repos/genomics-build.wiki"),
        llm_client=llm,
    )
    result = curator.curate()
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger("pinard.memory.wiki.curator")

# Configurable global wiki group_id — override for testing to avoid writing to
# the real production __global__ scope.
GLOBAL_WIKI_GROUP: str = os.environ.get("GLOBAL_WIKI_GROUP", "__global__")

_RESERVED_NAMES = {"index.md", "log.md"}
_DEDUP_SIMILARITY_THRESHOLD = 0.92  # cosine similarity above which we update rather than create


class WikiCuratorError(RuntimeError):
    pass


class WikiCurator:
    """Synthesize OKF wiki pages from the SurrealDB typed graph (outbound).

    Args:
        group_id: Tenant scope (matches the SurrealDB database / composed ontology).
        surreal: An open SurrealClient instance scoped to *group_id*.
        embed_fn: Callable(str) -> list[float]; embed a body string.
        composed: ComposedOntology for *group_id*.
        repo_path: Filesystem path to the cloned wiki git repository.
        llm_client: LLMClient instance (from build_llm_client()).
        gitlab_repo: GitLab project path for MR creation (optional).
        dry_run: If True, skip git push and MR calls.
        gitlab_host: GitLab API host (default: gitlab.com).
        bot_token: GitLab API token for MR creation (PRIVATE-TOKEN header).
    """

    def __init__(
        self,
        group_id: str,
        surreal: Any,
        embed_fn: Callable[[str], list[float]],
        composed: Any,
        repo_path: Path,
        llm_client: Any,
        gitlab_repo: str = "",
        dry_run: bool = False,
        gitlab_host: str = "",
        bot_token: str = "",
    ) -> None:
        self.group_id = group_id
        self._surreal = surreal
        self._embed = embed_fn
        self._composed = composed
        self._repo_path = Path(repo_path)
        self._llm = llm_client
        self._gitlab_repo = gitlab_repo
        self._dry_run = dry_run
        self._gitlab_host = gitlab_host or os.environ.get("GITLAB_HOST", "gitlab.com")
        self._bot_token = bot_token or os.environ.get("GITLAB_TOKEN", "")

        self._entity_roles: set[str] = set(composed.entity_roles()) if composed is not None else set()
        self._edge_valid_pairs: dict[str, set[tuple[str, str]]] = (
            {
                name: set(map(tuple, pairs))
                for name, pairs in composed.edge_type_map.items()
            }
            if composed is not None
            else {}
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def curate(self) -> dict[str, int]:
        """Run the full curation pipeline.

        Returns counts: synthesized, skipped, errors, mr_opened.
        """
        counts = {"synthesized": 0, "skipped": 0, "errors": 0, "mr_opened": 0}

        entities = self._select_source_material()
        if not entities:
            logger.info("[%s] No changed entities since last cursor — running snapshot only", self.group_id)

        # Phase 1 (LLM synthesis): only when entities have changed since the cursor.
        # Paths written by LLM synthesis this cycle (new/updated pages).
        newly_written: list[str] = []

        if entities:
            clusters = self._cluster_concepts(entities)
            logger.info(
                "Curating %d concept cluster(s) from %d changed entity/ies",
                len(clusters), len(entities),
            )
            for cluster in clusters:
                try:
                    result = self._process_cluster(cluster)
                    if result:
                        newly_written.append(result)
                        counts["synthesized"] += 1
                    else:
                        counts["skipped"] += 1
                except Exception:
                    logger.exception("Error processing cluster %s", self._cluster_label(cluster))
                    counts["errors"] += 1

        # Phase 2 (snapshot): always runs, even on quiet cycles with no changed entities.
        # Emit ALL previously-persisted auto_serve wiki_doc rows to disk so the curator
        # branch is a complete snapshot.  This ensures that after a pod restart (cursors
        # already current, 0 changed entities) a branch is still created/maintained for
        # vignes that have auto_serve decisions but no new activity.
        # Newly-synthesized pages (in newly_written) are not yet in wiki_doc (sync_in
        # upserts them after git-merge), so we merge both sets.
        all_docs = self._fetch_all_auto_serve_wiki_docs()
        snapshot_paths = self._emit_all_wiki_docs(all_docs)
        # Union: prefer the newly-synthesized paths (already on disk), then add
        # any persisted paths not covered by this cycle.
        all_written_paths = list(dict.fromkeys(newly_written + snapshot_paths))
        self._prune_stale_wiki_files(all_written_paths)

        if all_written_paths:
            mr_url = self._commit_and_push_branch(all_written_paths)
            if mr_url:
                counts["mr_opened"] += 1

        # Advance the cursor to *now* (server-side time::now() in _set_cursor).
        # Only advance when there were entities to process so the next run won't
        # re-select the same entities.  Using server-side time avoids the nanosecond
        # truncation that makes a Python-read entity timestamp always < its stored value.
        if entities:
            self._set_cursor()

        return counts

    # ── Source material ───────────────────────────────────────────────────────

    def _select_source_material(self) -> list[dict[str, Any]]:
        """Query entities (+ their outbound edges) changed since the stored cursor.

        The cursor comparison is done **entirely server-side** using a SurrealQL
        LET subquery so that both ``updated_at`` and ``last_synced_at`` keep
        full nanosecond precision — they are never truncated through Python.
        If no cursor record exists (first run) all entities are returned.

        Returns a flat list of entity dicts, each with an injected '_edges' key
        listing its typed outbound edges.
        """
        sql = (
            "LET $cur = (SELECT VALUE last_synced_at "
            "FROM type::record('wiki_curator_cursor', $src))[0]; "
            "SELECT *, updated_at FROM entity "
            "WHERE $cur IS NONE OR updated_at > $cur "
            "ORDER BY updated_at ASC LIMIT 500"
        )
        rows = self._surreal.query(sql, {"src": self._cursor_source()})

        # The LET statement produces a leading None in the result list;
        # the SELECT result is always the last element.
        result = rows[-1] if rows else None
        if not result:
            return []
        entities = result if isinstance(result, list) else [result]

        # Attach outbound edges for each entity.
        for ent in entities:
            ent["_edges"] = self._fetch_edges(ent)

        return entities

    def _fetch_edges(self, entity: dict[str, Any]) -> list[dict[str, Any]]:
        """Return typed outbound edges for an entity record."""
        ent_id = entity.get("id")
        if not ent_id:
            return []
        edges: list[dict[str, Any]] = []
        for edge_name in self._edge_valid_pairs:
            table = _camel_to_snake(edge_name)
            try:
                sql = (
                    f"SELECT ->{table}->entity.* AS targets FROM $ent_id LIMIT 1"
                )
                rows = self._surreal.query(sql, {"ent_id": ent_id})
                if rows and rows[0]:
                    row = rows[0]
                    row = row[0] if isinstance(row, list) else row
                    targets = row.get("targets") if isinstance(row, dict) else None
                    if targets:
                        if not isinstance(targets, list):
                            targets = [targets]
                        for tgt in targets:
                            if isinstance(tgt, dict) and tgt.get("name"):
                                edges.append({
                                    "edge": edge_name,
                                    "target_name": tgt["name"],
                                    "target_role": tgt.get("role", ""),
                                })
            except Exception:
                logger.debug("Edge query for %s/%s failed", edge_name, ent_id, exc_info=True)
        return edges

    def _fetch_all_auto_serve_wiki_docs(self) -> list[dict[str, Any]]:
        """Return all auto_serve wiki_doc rows from SurrealDB for this scope.

        Each curator runs against its own SurrealDB database (scoped to group_id),
        so no group_id filter is needed or correct at the SQL level — wiki_doc has
        no top-level group_id column (group_id lives in frontmatter, and the DB is
        already isolated per scope).  Human-authored rows are excluded via a
        Python post-filter on frontmatter.source.
        """
        try:
            rows = self._surreal.query(
                "SELECT path, title, type, summary, body, frontmatter, confidence, status "
                "FROM wiki_doc WHERE status = 'auto_serve'",
            )
            docs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
            result = []
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                fm = doc.get("frontmatter") or {}
                if fm.get("source") == "human":
                    continue
                if doc.get("type") == "artifact":
                    continue
                result.append(doc)
            return result
        except Exception:
            logger.warning("_fetch_all_auto_serve_wiki_docs failed for %s", self.group_id, exc_info=True)
            return []

    def _emit_all_wiki_docs(self, docs: list[dict[str, Any]]) -> list[str]:
        """Write all wiki_doc rows to disk; return list of relative paths written.

        Skips any file whose on-disk frontmatter has source == 'human' (preserve
        human-authored pages).  Uses the stored frontmatter + body from SurrealDB
        — no LLM call.
        """
        written: list[str] = []
        for doc in docs:
            path = doc.get("path") or ""
            if not path:
                slug = _slugify(doc.get("title") or "unknown")
                role = doc.get("type") or "concept"
                path = f"{_pluralize(role)}/{slug}"

            md_file = self._repo_path / (path + ".md")

            # Never overwrite human-authored on-disk pages.
            if md_file.exists():
                existing_fm = self._read_frontmatter(md_file)
                if existing_fm.get("source") == "human":
                    logger.debug("_emit_all_wiki_docs: skipping human-authored page %s", path)
                    continue

            fm = dict(doc.get("frontmatter") or {})
            fm.pop("content_hash", None)
            fm.setdefault("title", doc.get("title", ""))
            fm.setdefault("type", doc.get("type", ""))
            fm.setdefault("summary", doc.get("summary", ""))
            fm.setdefault("confidence", doc.get("confidence", 0.75))
            fm.setdefault("status", "auto_serve")
            fm.setdefault("group_id", self.group_id)
            fm.setdefault("source", "curator")

            body = doc.get("body") or ""
            try:
                self._write_okf(md_file, fm, body)
                written.append(path)
            except Exception:
                logger.warning("_emit_all_wiki_docs: failed to write %s", path, exc_info=True)

        return written

    def _prune_stale_wiki_files(self, all_written_paths: list[str]) -> None:
        """Delete on-disk .md files that are no longer in wiki_doc for this group.

        Scans all subdirectories of repo_path for .md files, and removes any
        whose relative path (without .md) is not in all_written_paths — unless
        the file's frontmatter has source == 'human' (those are never deleted).
        Reserved names (index.md, log.md) are also skipped.
        """
        written_set = set(all_written_paths)
        for md_file in self._repo_path.rglob("*.md"):
            # Only consider files inside subdirectories (role dirs), not root.
            try:
                rel = md_file.relative_to(self._repo_path)
            except ValueError:
                continue
            if len(rel.parts) < 2:
                continue
            if rel.name in _RESERVED_NAMES:
                continue
            # Build the path key without .md suffix (e.g. "decisions/foo").
            path_key = str(rel.with_suffix(""))
            if path_key in written_set:
                continue
            # Preserve human-authored files.
            fm = self._read_frontmatter(md_file)
            if fm.get("source") == "human":
                continue
            # Only prune files that belong to this group_id (or have no group_id set).
            file_group = fm.get("group_id", "")
            if file_group and file_group != self.group_id:
                continue
            try:
                md_file.unlink()
                logger.info("_prune_stale_wiki_files: removed stale page %s", path_key)
            except Exception:
                logger.warning("_prune_stale_wiki_files: failed to remove %s", md_file, exc_info=True)

    # ── Clustering ────────────────────────────────────────────────────────────

    # ── Clustering constants ──────────────────────────────────────────────────

    _CLUSTER_SIMILARITY_THRESHOLD = 0.75  # cosine similarity above which entities merge
    _CLUSTER_MAX_SIZE = 12                # cap cluster size to keep LLM prompts manageable

    def _cluster_concepts(self, entities: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Group related entities into concept clusters by embedding similarity.

        Greedy agglomerative strategy:
        1. Embed each entity's ``name + description`` (reusing embeddings already
           computed by the ingester if present as ``_embedding`` on the entity,
           otherwise calling ``self._embed`` on-demand).
        2. Sort entities by (role, name) so results are deterministic.
        3. Iterate: assign each entity to the first existing cluster whose
           centroid has cosine similarity ≥ threshold with the entity embedding;
           if no match, start a new cluster.
        4. Cap cluster size at ``_CLUSTER_MAX_SIZE`` to keep LLM prompts bounded.

        Entities without an embedding (embed call failed) form their own cluster.
        """
        if not entities:
            return []

        # Sort for deterministic output.
        sorted_entities = sorted(entities, key=lambda e: (e.get("role", ""), e.get("name", "")))

        # Compute embeddings for each entity.
        embeddings: list[list[float] | None] = []
        for ent in sorted_entities:
            existing = ent.get("_embedding")
            if existing and isinstance(existing, list):
                embeddings.append(existing)
            else:
                text = f"{ent.get('name', '')}\n{ent.get('description', '')}".strip()
                try:
                    embeddings.append(self._embed(text) if text else None)
                except Exception:
                    logger.debug("Embed failed for entity %r — isolated cluster", ent.get("name"))
                    embeddings.append(None)

        # Greedy cluster assignment.
        clusters: list[list[dict[str, Any]]] = []
        cluster_centroids: list[list[float] | None] = []

        for ent, emb in zip(sorted_entities, embeddings):
            if emb is None:
                # Cannot compute similarity — isolated cluster.
                clusters.append([ent])
                cluster_centroids.append(None)
                continue

            best_idx: int | None = None
            best_score: float = 0.0

            for idx, centroid in enumerate(cluster_centroids):
                if centroid is None:
                    continue
                if len(clusters[idx]) >= self._CLUSTER_MAX_SIZE:
                    continue
                score = _cosine_similarity(emb, centroid)
                if score >= self._CLUSTER_SIMILARITY_THRESHOLD and score > best_score:
                    best_score = score
                    best_idx = idx

            if best_idx is not None:
                clusters[best_idx].append(ent)
                # Update centroid: mean of all embeddings in cluster.
                cluster_centroids[best_idx] = _mean_embedding(
                    [e for e in [cluster_centroids[best_idx]] + [emb] if e is not None]
                )
            else:
                clusters.append([ent])
                cluster_centroids.append(emb)

        return clusters

    def _cluster_label(self, cluster: list[dict[str, Any]]) -> str:
        if cluster:
            return cluster[0].get("name", "<unnamed>")
        return "<empty>"

    def _cluster_max_ts(self, cluster: list[dict[str, Any]]) -> "datetime | None":
        """Return the most recent updated_at in the cluster as a datetime object.

        SurrealDB returns ``updated_at`` as a Python :class:`datetime` (CBOR-decoded).
        Comparing datetime objects directly is correct and format-independent.
        """
        ts: "datetime | None" = None
        for ent in cluster:
            val = ent.get("updated_at")
            if val is None:
                continue
            # Normalise: SurrealDB CBOR returns a datetime; strings are a fallback.
            if not isinstance(val, datetime):
                try:
                    val = datetime.fromisoformat(str(val).replace(" ", "T"))
                except (ValueError, TypeError):
                    continue
            if ts is None or val > ts:
                ts = val
        return ts

    # ── Per-cluster processing ────────────────────────────────────────────────

    def _process_cluster(self, cluster: list[dict[str, Any]]) -> str | None:
        """Synthesize + write one concept cluster.

        Returns the OKF path (relative to repo_path) if written, None if skipped.
        """
        primary = cluster[0]
        name = primary.get("name", "")
        role = primary.get("role", "")
        description = primary.get("description", "")

        # Artifact-role clusters are excluded from wiki synthesis — they are raw
        # memory tier (redundant with Engram) and remain retrievable via recall.
        if role == "artifact":
            logger.debug("Skipping artifact-role cluster %r — excluded from wiki synthesis", name)
            return None

        # Derive stable concept path from raw name (pre-synthesis slug).
        slug = _slugify(name, max_len=80)
        if role and role in self._entity_roles:
            okf_path = f"{_pluralize(role)}/{slug}"
        else:
            okf_path = f"concepts/{slug}"

        md_file = self._repo_path / (okf_path + ".md")

        # Skip reserved names.
        if md_file.name in _RESERVED_NAMES:
            return None

        # Never overwrite human-authored pages.
        if md_file.exists():
            existing_fm = self._read_frontmatter(md_file)
            if existing_fm.get("source") == "human":
                logger.info("Skipping human-authored page: %s", okf_path)
                return None

        # Dedup: check embedding similarity vs existing wiki_doc.
        candidate_text = f"{name}\n{description}"
        candidate_embedding = self._embed(candidate_text) if candidate_text.strip() else None
        if candidate_embedding:
            existing = self._find_similar_wiki_doc(candidate_embedding)
            if existing and existing.get("path") != okf_path:
                logger.info(
                    "Near-duplicate concept %r matches existing page %r — updating existing",
                    okf_path, existing.get("path"),
                )
                okf_path = existing["path"]
                md_file = self._repo_path / (okf_path + ".md")
                # Still check human-authored on the matched page.
                if md_file.exists():
                    matched_fm = self._read_frontmatter(md_file)
                    if matched_fm.get("source") == "human":
                        logger.info("Skipping near-dup merge into human-authored page: %s", okf_path)
                        return None

        # Synthesize via LLM.
        frontmatter, body = self._synthesize_concept(cluster, okf_path)

        # Re-derive slug from LLM-synthesized title for a clean filename.
        synth_title = frontmatter.get("title") or name
        clean_slug = _slugify(synth_title, max_len=80)
        if role and role in self._entity_roles:
            okf_path = f"{_pluralize(role)}/{clean_slug}"
        else:
            okf_path = f"concepts/{clean_slug}"
        md_file = self._repo_path / (okf_path + ".md")

        self._write_okf(md_file, frontmatter, body)
        logger.info("Wrote OKF page: %s", okf_path)
        return okf_path

    # ── Synthesis ─────────────────────────────────────────────────────────────

    def _synthesize_concept(
        self,
        cluster: list[dict[str, Any]],
        okf_path: str,
    ) -> tuple[dict[str, Any], str]:
        """Call the LLM to produce OKF frontmatter + markdown body.

        Returns (frontmatter_dict, body_str).
        """
        primary = cluster[0]
        name = primary.get("name", "")
        role = primary.get("role", "")
        description = primary.get("description", "")
        edges = primary.get("_edges", [])

        # Determine ontology type.
        cluster_size = len(cluster)
        if role and role in self._entity_roles:
            ont_type = role
        else:
            ont_type = role or "concept"

        # Derive confidence from principled signals rather than defaulting to 0.6:
        #   base 0.60 + cluster-size bonus (log2 scale, capped) + role bonus
        # High-value typed roles (decision, diagnosis, verdict, log_pattern) get a 0.12 bonus
        # so a single well-typed entity reaches auto_serve (>=0.7). Artifact/unknown roles
        # require larger cluster sizes to reach auto_serve.
        #
        # Examples (cluster_size=1, size_bonus=log2(2)*0.05=0.05):
        #   decision  : 0.60 + 0.05 + 0.12 = 0.77 -> auto_serve
        #   diagnosis : 0.60 + 0.05 + 0.12 = 0.77 -> auto_serve
        #   artifact  : 0.60 + 0.05 + 0.00 = 0.65 -> needs_review
        #   unknown   : 0.60 + 0.05 + 0.00 = 0.65 -> needs_review
        # Examples (cluster_size=4, size_bonus=log2(5)*0.05~=0.116):
        #   artifact  : 0.60 + 0.116 + 0.00 = 0.716 -> auto_serve
        _HIGH_VALUE_ROLES = {"decision", "diagnosis", "verdict", "log_pattern"}
        role_bonus = 0.12 if (role in _HIGH_VALUE_ROLES) else 0.0
        size_bonus = 0.05 * math.log2(cluster_size + 1)
        confidence = round(min(0.9, 0.60 + size_bonus + role_bonus), 3)
        status = "auto_serve" if confidence >= 0.7 else "needs_review"

        # Build validated relations list.
        relations = self._build_relations(edges, ont_type)

        # Build LLM prompt — synthesize from the full cluster, not just the primary entity.
        system = (
            "You are a technical knowledge curator. Synthesize a concise OKF wiki page "
            "from all provided cluster members into one coherent concept page. "
            "Respond with a JSON object with exactly three keys: "
            "\"title\" (one clean human-readable phrase, no trailing colon, no markdown marks, no provenance parentheticals), "
            "\"summary\" (one distilled sentence suitable as a boot-manifest hint), "
            "and \"body\" (a markdown body using sections # Overview, # Details, # Citations; "
            "link to sources as [name](path)). "
            "If multiple cluster members describe the same concept from different angles, "
            "synthesize them into a unified, non-repetitive account. "
            "Output only the JSON object — no other text."
        )
        entities_desc = "\n".join(
            f"- [{ent.get('role', 'artifact')}] {ent.get('name', '')}: {ent.get('description', '')}"
            for ent in cluster
        )
        edges_desc = "\n".join(
            f"- {e['edge']} → {e['target_name']}" for e in edges
        ) or "none"
        cluster_note = (
            f"This concept is supported by {cluster_size} related observation(s).\n"
            if cluster_size > 1
            else ""
        )
        user_msg = textwrap.dedent(f"""\
            Synthesize a wiki page for the concept: **{name}**

            Type: {ont_type}
            Primary description: {description}

            {cluster_note}\
            All cluster members (synthesize these into one page):
            {entities_desc}

            Related entities (outbound edges from primary):
            {edges_desc}

            Be concise and factual. Do not just list items — write prose that synthesizes the knowledge.
            Respond with a JSON object: {{"title": "...", "summary": "...", "body": "..."}}
        """)

        synth_title = name
        synth_summary = ""
        body = ""
        try:
            raw = self._llm.complete(
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=1200,
                system=system,
            )
            parsed = _parse_synthesis_response(raw)
            synth_title = parsed.get("title") or name
            synth_summary = parsed.get("summary") or ""
            body = parsed.get("body") or ""
        except Exception as exc:
            logger.warning("LLM synthesis failed for %r: %s — using description fallback", name, exc)

        if not body:
            body = f"# Overview\n\n{description or name}\n"
        if not synth_summary:
            # Cheap fallback: first sentence of description.
            first = (description or name).split(".")[0].strip()
            synth_summary = (first + ".") if first and not first.endswith(".") else (first or name)

        frontmatter: dict[str, Any] = {
            "type": ont_type,
            "title": synth_title,
            "summary": synth_summary,
            "group_id": self.group_id,
            "confidence": round(confidence, 3),
            "status": status,
            "source": "curator",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        if relations:
            frontmatter["relations"] = relations

        return frontmatter, body

    def _build_relations(
        self,
        edges: list[dict[str, Any]],
        src_type: str,
    ) -> list[dict[str, str]]:
        """Build a validated relations list for the frontmatter.

        Only emits edges whose (src_type, tgt_role) pair is in the ontology.
        Unknown/invalid edges are silently skipped (will be staged by sync_in on round-trip).
        """
        relations = []
        seen: set[tuple[str, str]] = set()
        for edge in edges:
            edge_name = edge.get("edge", "")
            target_name = edge.get("target_name", "")
            target_role = edge.get("target_role", "")
            if not edge_name or not target_name:
                continue
            key = (edge_name, target_name)
            if key in seen:
                continue
            seen.add(key)
            valid_pairs = self._edge_valid_pairs.get(edge_name)
            if valid_pairs is None:
                logger.debug("Unknown edge type %r — omitting from relations", edge_name)
                continue
            if src_type not in self._entity_roles:
                logger.debug("src_type %r not in ontology — omitting edge %r", src_type, edge_name)
                continue
            matching = [p for p in valid_pairs if p[0] == src_type]
            if not matching:
                logger.debug(
                    "Edge %r not valid for src_type %r — omitting", edge_name, src_type
                )
                continue
            target_slug = _slugify(target_name)
            if target_role and target_role in self._entity_roles:
                target_path = f"{_pluralize(target_role)}/{target_slug}"
            else:
                target_path = f"concepts/{target_slug}"
            relations.append({"edge": edge_name, "to": target_path})
        return relations

    # ── Dedup ─────────────────────────────────────────────────────────────────

    def _find_similar_wiki_doc(self, embedding: list[float]) -> dict[str, Any] | None:
        """Return the most similar existing wiki_doc if above the dedup threshold."""
        try:
            rows = self._surreal.query(
                "SELECT path, vector::similarity::cosine(embedding, $vec) AS score "
                "FROM wiki_doc WHERE embedding IS NOT NULL "
                "ORDER BY score DESC LIMIT 1",
                {"vec": embedding},
            )
            if not rows or not rows[0]:
                return None
            row = rows[0]
            row = row[0] if isinstance(row, list) else row
            if isinstance(row, dict):
                score = float(row.get("score", 0.0))
                if score >= _DEDUP_SIMILARITY_THRESHOLD:
                    return row
        except Exception:
            logger.debug("Dedup similarity query failed", exc_info=True)
        return None

    # ── OKF write ─────────────────────────────────────────────────────────────

    def _write_okf(
        self,
        md_file: Path,
        frontmatter: dict[str, Any],
        body: str,
    ) -> None:
        """Write (or overwrite) an OKF markdown file at md_file."""
        if yaml is None:
            raise WikiCuratorError("PyYAML is required to write OKF frontmatter")
        md_file.parent.mkdir(parents=True, exist_ok=True)
        fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
        content = f"---\n{fm_str}---\n{body}"
        md_file.write_text(content, encoding="utf-8")

    def _read_frontmatter(self, md_file: Path) -> dict[str, Any]:
        """Parse and return the YAML frontmatter of an existing OKF page."""
        if yaml is None:
            return {}
        try:
            raw = md_file.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not m:
                return {}
            return yaml.safe_load(m.group(1)) or {}
        except Exception:
            return {}

    # ── Cursor tracking ───────────────────────────────────────────────────────

    def _cursor_source(self) -> str:
        return f"wiki_curator:{self.group_id}"

    def _get_cursor(self) -> "datetime | None":
        """Return the last cursor timestamp for this group_id as a datetime, or None.

        SurrealDB returns ``last_synced_at`` as a :class:`datetime` when stored
        correctly (CBOR-decoded).  Strings are normalised to datetime so the
        comparison in :meth:`_select_source_material` is always datetime↔datetime.
        """
        try:
            rows = self._surreal.query(
                "SELECT last_synced_at FROM type::record('wiki_curator_cursor', $src) LIMIT 1",
                {"src": self._cursor_source()},
            )
            if rows and rows[0]:
                row = rows[0]
                row = row[0] if isinstance(row, list) else row
                if isinstance(row, dict):
                    val = row.get("last_synced_at")
                    if val is None:
                        return None
                    if isinstance(val, datetime):
                        return val
                    # Fallback: normalise string (space-separated or ISO-8601).
                    try:
                        return datetime.fromisoformat(str(val).replace(" ", "T"))
                    except (ValueError, TypeError):
                        logger.warning("Unrecognised cursor format %r — ignoring", val)
                        return None
        except Exception:
            logger.debug("Could not read wiki_curator_cursor", exc_info=True)
        return None

    def _set_cursor(self) -> None:
        """Persist the cursor to *now* using a server-side ``time::now()``.

        Storing the cursor as ``time::now()`` (evaluated on the SurrealDB server)
        ensures full nanosecond precision.  Any entity whose ``updated_at`` was
        set before this call will have ``updated_at < last_synced_at`` on the
        next run, regardless of Python’s microsecond truncation.
        """
        try:
            self._surreal.query(
                "UPSERT type::record('wiki_curator_cursor', $src) SET "
                "source = $src, last_synced_at = time::now(), updated_at = time::now()",
                {"src": self._cursor_source()},
            )
        except Exception:
            logger.warning("Could not persist wiki_curator_cursor", exc_info=True)

    # ── Git / MR operations ───────────────────────────────────────────────────

    def _branch_name(self) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "-", self.group_id)
        return f"wiki-curator/{safe}"

    def _commit_and_push_branch(self, written_paths: list[str]) -> str | None:
        """git add + commit + push the written pages; open or refresh an MR.

        Returns the MR URL or None.
        """
        branch = self._branch_name()
        repo = self._repo_path

        # Ensure on the right branch (create if missing, else just switch).
        self._run(["git", "fetch", "origin"], repo, check=False)
        remote_exists = self._run(
            ["git", "ls-remote", "--exit-code", "origin", branch],
            repo, check=False,
        ).returncode == 0

        # Use -B unconditionally: creates the branch if new, or resets it to HEAD
        # if it already exists locally — safe even when the working tree is dirty
        # (files are written after this point, so there is nothing to carry over).
        # Do NOT pull --rebase here: the curator regenerates pages from scratch each
        # run, so the branch is always bot-owned and force-pushed; a rebase on a
        # dirty tree would fail anyway (git refuses to rebase with unstaged changes).
        self._run(["git", "checkout", "-B", branch], repo)

        # Stage all written files.
        for rel_path in written_paths:
            md_file = repo / (rel_path + ".md")
            if md_file.exists():
                self._run(["git", "add", str(md_file)], repo)

        # Check if there's anything to commit.
        status = self._run(["git", "diff", "--cached", "--name-only"], repo, check=False)
        if not status.stdout.strip():
            logger.info("Nothing new to commit for wiki-curator branch %s", branch)
            return None

        commit_msg = (
            f"feat(wiki): full snapshot — {len(written_paths)} page(s) [{self.group_id}]\n\n"
            + "\n".join(f"- {p}" for p in written_paths)
        )
        self._run(["git", "commit", "--no-verify", "-m", commit_msg], repo)

        if self._dry_run:
            logger.info("[DRY_RUN] Would push branch %s and open MR", branch)
            return "dry-run://mr/0"

        # Detect the default branch of the remote (master vs main).
        default_branch = self._detect_default_branch(repo)

        title = f"wiki(curator): full wiki snapshot for {self.group_id}"

        # Force-push with lease + GitLab push options to create/refresh the MR
        # in a single round-trip over the existing SSH push. This eliminates the
        # need for an API token, a pre-known repo path, or a specific target-branch
        # assumption. GitLab deduplicates by source branch (re-push is idempotent).
        # Push options are silently ignored by servers that don't support them.
        push_cmd = [
            "git", "push", "--force-with-lease", "-u", "origin", branch,
            "-o", "merge_request.create",
            "-o", f"merge_request.target={default_branch}",
            "-o", f"merge_request.title={title}",
            "-o", "merge_request.remove_source_branch=false",
        ]
        push_result = self._run(push_cmd, repo, check=False)
        if push_result.returncode != 0:
            logger.error("git push failed: %s", push_result.stderr.strip())
            return None

        # Extract the MR URL from the push output (GitLab prints it on stderr).
        mr_url = self._extract_mr_url_from_push(push_result.stderr)
        if mr_url:
            logger.info("MR for %s: %s (via push options)", self.group_id, mr_url)
            return mr_url

        # Fallback: if a bot API token is configured, open the MR via REST.
        # This path is used when push options are unsupported or the URL was not
        # printed (e.g. old GitLab, dry-push). Derives the project path from the
        # remote URL so it works for any per-vignoble repo without a config entry.
        return self._open_mr_via_rest(branch, default_branch, title)

    def _detect_default_branch(self, repo: Path) -> str:
        """Return the default branch name of the remote (master, main, …)."""
        result = self._run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            repo, check=False,
        )
        ref = result.stdout.strip()
        if ref:
            # e.g. "refs/remotes/origin/master" → "master"
            return ref.rsplit("/", 1)[-1] or "master"
        # Fallback: inspect the remote's HEAD directly.
        result2 = self._run(
            ["git", "remote", "show", "origin"],
            repo, check=False,
        )
        for line in result2.stdout.splitlines():
            if "HEAD branch" in line:
                return line.split(":", 1)[-1].strip() or "master"
        return "master"

    @staticmethod
    def _extract_mr_url_from_push(stderr: str) -> str | None:
        """Pull the MR URL out of GitLab's push-output stderr."""
        for line in stderr.splitlines():
            stripped = line.strip().lstrip("remote:").strip()
            if stripped.startswith("https://") and "/merge_requests/" in stripped:
                return stripped
        return None

    def _remote_project_path(self, repo: Path) -> str:
        """Derive the GitLab project path from the clone's remote URL.

        Handles both SSH (``git@ssh.HOST:GROUP/PROJ.git``) and HTTPS remotes.
        """
        result = self._run(["git", "remote", "get-url", "origin"], repo, check=False)
        url = result.stdout.strip()
        # SSH: git@ssh.github.com:your-org/pinard-wiki.git
        if ":" in url and not url.startswith("http"):
            path = url.split(":", 1)[-1]
        else:
            # HTTPS: https://gitlab.com/group/proj.git
            from urllib.parse import urlparse
            path = urlparse(url).path.lstrip("/")
        return path.removesuffix(".git")

    def _open_mr_via_rest(self, branch: str, default_branch: str, title: str) -> str | None:
        """Open or refresh a GitLab MR via the REST API (fallback path).

        Used only when a bot API token is available (GITLAB_TOKEN env var).
        The project path is derived from the clone's remote URL so it works
        for any per-vignoble repo without a hard-coded config entry.
        """
        if not self._bot_token:
            logger.debug(
                "No GITLAB_TOKEN — MR creation skipped for %s (push succeeded via push options)",
                self.group_id,
            )
            return None

        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed — cannot open MR via REST")
            return None

        # Derive project path from remote URL (avoids hard-coded per-vignoble config).
        project_path = self._remote_project_path(self._repo_path)
        if not project_path:
            logger.error("Could not derive project path from remote URL for %s", self.group_id)
            return None

        encoded_repo = project_path.replace("/", "%2F")
        api_url = f"https://{self._gitlab_host}/api/v4/projects/{encoded_repo}/merge_requests"
        description = (
            f"## Wiki curator — {self.group_id}\n\n"
            f"Auto-generated OKF pages synthesized from the SurrealDB typed graph.\n\n"
            f"- Branch: `{branch}`\n"
            f"- Group: `{self.group_id}`\n\n"
            f"**Review these pages before merging.** "
            f"Curator drafts are `needs_review` by default.\n"
        )
        payload = {
            "source_branch": branch,
            "target_branch": default_branch,
            "title": title,
            "description": description,
            "remove_source_branch": False,
        }
        headers = {"PRIVATE-TOKEN": self._bot_token}

        try:
            resp = httpx.post(api_url, json=payload, headers=headers, timeout=15)
            if resp.status_code in (200, 201):
                data = resp.json()
                url = data.get("web_url", "")
                logger.info("Opened MR for %s via REST: %s", self.group_id, url)
                return url
            if resp.status_code == 409:
                # MR already exists — fetch its URL.
                existing = httpx.get(
                    f"{api_url}?source_branch={branch}&state=opened",
                    headers=headers, timeout=10,
                )
                if existing.status_code == 200:
                    items = existing.json()
                    if items:
                        url = items[0].get("web_url", "")
                        logger.info("MR already exists for %s: %s", self.group_id, url)
                        return url
                return None
            logger.error(
                "GitLab MR creation failed for %s (HTTP %d): %s",
                self.group_id, resp.status_code, resp.text[:200],
            )
            return None
        except Exception as exc:
            logger.error("MR creation REST request failed for %s: %s", self.group_id, exc)
            return None

    def _run(
        self,
        cmd: list[str],
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        logger.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            check=check,
            capture_output=True,
            text=True,
        )


# ── Multi-vignoble entry point ───────────────────────────────────────────────

def curate_all_vignobles(
    vignobles_base_dir: Path | str,
    global_wiki_root: Path | str | None,
    embed_fn: Callable[[str], list[float]],
    llm_client: Any,
    registry: Any,
    gitlab_repo: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Curate wiki pages for every vignoble found under *vignobles_base_dir*.

    For each subdirectory ``vignoble-<name>`` under *vignobles_base_dir*, reads
    ``vignes.yaml`` to discover group_ids (vignes) and runs :class:`WikiCurator`
    for each group_id, writing OKF pages to ``<vignoble-dir>/wiki/``.

    If *global_wiki_root* is set, also curates the ``__global__`` SurrealDB scope
    into that directory.

    Best-effort: a failure for one vignoble or group_id does not abort the rest.

    Args:
        vignobles_base_dir: Parent directory whose subdirs are vignoble clones
            (e.g. ``/data/repos/vignobles``).
        global_wiki_root: Path to the global pinard-wiki clone (may be None).
        embed_fn: Embedding callable.
        llm_client: LLMClient instance.
        registry: OntologyRegistry instance.
        gitlab_repo: GitLab project path for ``glab mr create`` (optional).
        dry_run: Skip git push + glab when True.

    Returns:
        Aggregated counts ``{vignoble_name: {group_id: {synthesized, skipped, errors, mr_opened}}}``.
    """
    from services.memory.surrealdb.client import SurrealClient  # local import to avoid circular

    base = Path(vignobles_base_dir)
    results: dict[str, Any] = {}

    if base.exists():
        for vignoble_dir in sorted(base.iterdir()):
            if not vignoble_dir.is_dir():
                continue
            vignoble_name = vignoble_dir.name  # e.g. "vignoble-misc"

            vignes_yaml = vignoble_dir / "vignes.yaml"
            if not vignes_yaml.exists():
                logger.debug("No vignes.yaml in %s — skipping", vignoble_dir)
                continue

            try:
                with open(vignes_yaml) as f:
                    vignes_data = yaml.safe_load(f) or {}
            except Exception as exc:
                logger.warning("Failed to read %s: %s — skipping vignoble", vignes_yaml, exc)
                continue

            group_ids = list((vignes_data.get("vignes") or {}).keys())
            wiki_dir = vignoble_dir / "wiki"

            results[vignoble_name] = {}

            for group_id in group_ids:
                try:
                    composed = registry.compose(group_id)
                    with SurrealClient(group_id=group_id) as surreal:
                        surreal.ensure_schema(registry=registry, group_id=group_id)
                        curator = WikiCurator(
                            group_id=group_id,
                            surreal=surreal,
                            embed_fn=embed_fn,
                            composed=composed,
                            repo_path=wiki_dir / group_id,
                            llm_client=llm_client,
                            gitlab_repo=gitlab_repo,
                            dry_run=dry_run,
                        )
                        counts = curator.curate()
                        results[vignoble_name][group_id] = counts
                        logger.info(
                            "Curated vignoble=%s group=%s: %s",
                            vignoble_name, group_id, counts,
                        )
                except Exception:
                    logger.exception(
                        "Error curating vignoble=%s group=%s — continuing",
                        vignoble_name, group_id,
                    )
                    results[vignoble_name][group_id] = {"errors": 1}
    else:
        logger.warning(
            "curate_all_vignobles: vignobles base dir %s does not exist — skipping vignoble curation, continuing to global",
            base,
        )

    if global_wiki_root:
        global_path = Path(global_wiki_root)
        if global_path.exists():
            global_group_id = GLOBAL_WIKI_GROUP
            try:
                composed = registry.compose(global_group_id)
                with SurrealClient(group_id=global_group_id) as surreal:
                    surreal.ensure_schema(registry=registry, group_id=global_group_id)
                    curator = WikiCurator(
                        group_id=global_group_id,
                        surreal=surreal,
                        embed_fn=embed_fn,
                        composed=composed,
                        repo_path=global_path,
                        llm_client=llm_client,
                        gitlab_repo=gitlab_repo,
                        dry_run=dry_run,
                    )
                    counts = curator.curate()
                    results[global_group_id] = {global_group_id: counts}
                    logger.info("Curated global wiki: %s", counts)
            except Exception:
                logger.exception("Error curating global wiki — continuing")
                results[global_group_id] = {global_group_id: {"errors": 1}}
        else:
            logger.debug("global_wiki_root %s does not exist — skipping global curation", global_path)

    return results


def sync_out_vignoble_shared(
    vignobles_base_dir: Path | str,
    embed_fn: Callable[[str], list[float]],
    gitlab_repo: str = "",
    dry_run: bool = False,
    gitlab_host: str = "",
    bot_token: str = "",
) -> dict[str, Any]:
    """Write vignoble-scoped wiki_doc rows to ``wiki/_shared/`` in the wiki git repo.

    For each vignoble under *vignobles_base_dir*, opens the ``_vignoble_db`` SurrealDB
    scope, selects all ``wiki_doc`` rows with ``status = 'auto_serve'`` and
    ``frontmatter.source != 'human'``, and writes each to
    ``<vignoble-dir>/wiki/_shared/{role}s/{slug}.md``.

    Pages are committed and pushed via the existing branch mechanism
    (branch ``wiki-curator/_shared``) with a GitLab MR opened or refreshed.

    Human-authored pages (``frontmatter.source == 'human'``) are never overwritten.
    Best-effort: a failure for one vignoble does not abort the rest.

    Args:
        vignobles_base_dir: Parent directory whose subdirs are vignoble clones.
        embed_fn: Embedding callable (unused here but kept for signature consistency).
        gitlab_repo: GitLab project path for MR creation (optional).
        dry_run: Skip git push + glab when True.
        gitlab_host: GitLab API host (e.g. gitlab.example.com).
        bot_token: GitLab API token for MR creation (optional).

    Returns:
        ``{vignoble_name: {written, skipped, errors, mr_opened}}``
    """
    from services.memory.surrealdb.client import SurrealClient  # local import to avoid circular
    from services.memory.rollup import _vignoble_db, VIGNOBLE_DB_PREFIX  # type: ignore[import]

    base = Path(vignobles_base_dir)
    results: dict[str, Any] = {}

    if not base.exists():
        logger.warning(
            "sync_out_vignoble_shared: vignobles base dir %s does not exist — skipping", base
        )
        return results

    for vignoble_dir in sorted(base.iterdir()):
        if not vignoble_dir.is_dir():
            continue
        vignoble_name = vignoble_dir.name
        if not (vignoble_dir / "vignes.yaml").exists():
            logger.debug("No vignes.yaml in %s — skipping", vignoble_dir)
            continue

        wiki_dir = vignoble_dir / "wiki"
        if not wiki_dir.exists():
            logger.debug("wiki dir %s does not exist — skipping vignoble %s", wiki_dir, vignoble_name)
            results[vignoble_name] = {"written": 0, "skipped": 0, "errors": 0, "mr_opened": 0}
            continue

        shared_dir = wiki_dir / "_shared"
        vname = vignoble_name[len(VIGNOBLE_DB_PREFIX):] if vignoble_name.startswith(VIGNOBLE_DB_PREFIX) else vignoble_name
        db_name = _vignoble_db(vname)
        counts: dict[str, int] = {"written": 0, "skipped": 0, "errors": 0, "mr_opened": 0}
        written_paths: list[str] = []

        try:
            with SurrealClient(group_id=db_name) as surreal:
                rows = surreal.query(
                    "SELECT path, title, type, summary, body, frontmatter, confidence, embedding "
                    "FROM wiki_doc WHERE status = 'auto_serve'",
                )
                docs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])

                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    fm = doc.get("frontmatter") or {}
                    if fm.get("source") == "human":
                        counts["skipped"] += 1
                        continue

                    path = doc.get("path") or ""
                    if not path:
                        slug = _slugify(doc.get("title") or "unknown")
                        role = doc.get("type") or "concept"
                        path = f"{_pluralize(role)}/{slug}"

                    md_file = shared_dir / (path + ".md")

                    # Never overwrite human-authored files on disk.
                    if md_file.exists():
                        try:
                            raw = md_file.read_text(encoding="utf-8")
                            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
                            if m and yaml is not None:
                                disk_fm = yaml.safe_load(m.group(1)) or {}
                                if disk_fm.get("source") == "human":
                                    counts["skipped"] += 1
                                    continue
                        except Exception:
                            pass

                    try:
                        out_fm = dict(fm)
                        out_fm.setdefault("title", doc.get("title", ""))
                        out_fm.setdefault("type", doc.get("type", ""))
                        out_fm.setdefault("summary", doc.get("summary", ""))
                        out_fm.setdefault("confidence", doc.get("confidence", 0.75))
                        out_fm["source"] = "rollup-curator"
                        out_fm["wiki_scope"] = "vignoble-shared"

                        body = doc.get("body") or ""
                        md_file.parent.mkdir(parents=True, exist_ok=True)
                        if yaml is None:
                            raise WikiCuratorError("PyYAML is required to write OKF frontmatter")
                        fm_str = yaml.dump(out_fm, default_flow_style=False, allow_unicode=True)
                        md_file.write_text(f"---\n{fm_str}---\n{body}", encoding="utf-8")

                        written_paths.append(path)
                        counts["written"] += 1
                        logger.info("sync_out_vignoble_shared: wrote %s → %s", path, md_file)
                    except Exception:
                        logger.exception(
                            "sync_out_vignoble_shared: error writing %s for vignoble=%s",
                            path, vignoble_name,
                        )
                        counts["errors"] += 1

        except Exception:
            logger.exception(
                "sync_out_vignoble_shared: error reading vignoble scope %s — continuing",
                vignoble_name,
            )
            counts["errors"] += 1
            results[vignoble_name] = counts
            continue

        if written_paths and wiki_dir.exists():
            try:
                # Reuse WikiCurator's git/MR helpers by borrowing a minimal instance.
                # group_id="_shared" makes _branch_name() return "wiki-curator/_shared"
                # naturally (underscore is preserved by the regex), so no monkeypatching.
                _curator = WikiCurator(
                    group_id="_shared",
                    surreal=None,  # type: ignore[arg-type]  # not used — no curate() call
                    embed_fn=embed_fn,
                    composed=None,  # type: ignore[arg-type]
                    repo_path=wiki_dir,
                    llm_client=None,  # type: ignore[arg-type]
                    gitlab_repo=gitlab_repo,
                    dry_run=dry_run,
                    gitlab_host=gitlab_host,
                    bot_token=bot_token,
                )
                mr_url = _curator._commit_and_push_branch(
                    [f"_shared/{p}" for p in written_paths]
                )
                if mr_url:
                    counts["mr_opened"] += 1
            except Exception:
                logger.exception(
                    "sync_out_vignoble_shared: git/MR step failed for vignoble=%s — continuing",
                    vignoble_name,
                )
                counts["errors"] += 1

        results[vignoble_name] = counts
        logger.info("sync_out_vignoble_shared vignoble=%s: %s", vignoble_name, counts)

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

_ROLE_PLURAL: dict[str, str] = {
    "diagnosis": "diagnoses",
    "analysis": "analyses",
    "hypothesis": "hypotheses",
}


def _pluralize(role: str) -> str:
    """Return the plural directory name for a given entity role."""
    return _ROLE_PLURAL.get(role, role + "s")


def _slugify(text: str, max_len: int = 0) -> str:
    """Convert a name to a filesystem-safe slug.

    If max_len > 0, truncate at a word boundary (on "-") so the result is at
    most max_len characters, avoiding mid-word cuts.
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug or "unknown"
    if max_len > 0 and len(slug) > max_len:
        truncated = slug[:max_len]
        last_dash = truncated.rfind("-")
        slug = truncated[:last_dash] if last_dash > 0 else truncated
        slug = slug.strip("-") or "unknown"
    return slug


def _parse_synthesis_response(raw: str) -> dict[str, str]:
    """Extract {title, summary, body} from an LLM JSON response.

    Tries to parse the entire response as JSON first; if that fails, looks for
    a JSON object embedded anywhere in the text (e.g. wrapped in markdown fences).
    Returns a (possibly partial) dict — callers handle missing keys.
    """
    text = raw.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence (and optional language tag) and closing fence.
        inner_lines = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner_lines.append(line)
        text = "\n".join(inner_lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return {k: str(v) for k, v in result.items() if k in ("title", "summary", "body")}
    except (json.JSONDecodeError, ValueError):
        pass
    # Last-ditch: find the first {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, dict):
                return {k: str(v) for k, v in result.items() if k in ("title", "summary", "body")}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case for edge table names."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mean_embedding(embeddings: list[list[float]]) -> list[float] | None:
    """Return the component-wise mean of a list of equal-length embeddings."""
    if not embeddings:
        return None
    dim = len(embeddings[0])
    result = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            result[i] += v
    n = len(embeddings)
    return [v / n for v in result]
