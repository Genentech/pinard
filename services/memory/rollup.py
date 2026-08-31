"""Scope roll-up engine for the pinard memory layer.

Reads vignes.yaml to discover which group_ids (vignes) belong to each
vignoble, then aggregates cross-vigne knowledge into vignoble-level and
global-level namespaces via curate-on-promote.

Invariant: **No entity record (any role) is ever copied to a higher scope.**
Only curated knowledge (wiki_doc) rises.  Promotion detects cross-vigne overlap
of wiki_doc pages and typed entities via **embedding-similarity clustering**
(cosine ≥ CLUSTER_SIMILARITY_THRESHOLD), then synthesises a new consolidated
wiki_doc at the higher-tier scope via the configured LLM.  Existing entity rows
that may have leaked into higher-tier scopes in earlier releases are cleaned up
on every run.

Scope naming follows Engram's project nomenclature (group_id == Engram project, 1:1):
  - vigne scope:    DB = group_id          (e.g. "pinard", "genomics-build", "exo-cli")
  - vignoble scope: DB = "vignoble-<name>" (e.g. "vignoble-exohub", "vignoble-misc")
  - parcelle scope: DB = "parcelle-<name>" (e.g. "parcelle-memory") — orthogonal axis
  - global scope:   DB = "__global__"      (dunder prefix/suffix — reserved, collision-proof)

Parcelle scopes are a first-class, separate axis from the vigne→vignoble→global
hierarchy. A parcelle can span multiple vignes (it is a maître workstream, not a
vigne), so its entities are stored independently and do not roll up into vignoble.

Curate-on-promote: knowledge items (wiki_docs or typed entities) from all group_ids
in a vignoble are pooled and clustered by embedding similarity.  Any cluster that
spans 2+ distinct vignes triggers synthesis of a consolidated wiki_doc at the
vignoble scope.  The same logic repeats across vignobles for the global scope.
Synthesis requires an LLM client and embed function; when not provided the engine
still runs cleanup but skips synthesis (logged as a warning).

Environment variables:
    VIGNOBLE_YAML        — Path to vignes.yaml (default: $VIGNOBLE_DIR/vignes.yaml)
    VIGNOBLE_DIR         — Vignoble root directory (used to locate vignes.yaml)
    VIGNOBLES_BASE_DIR   — Parent dir of multiple vignoble clones (e.g. /data/repos/vignobles).
                           When set, all subdirs with a vignes.yaml are iterated, superseding
                           VIGNOBLE_DIR/VIGNOBLE_YAML for membership discovery.
    SURREAL_URL          — SurrealDB endpoint
    SURREAL_USER         — SurrealDB username
    SURREAL_PASS         — SurrealDB password
    ROLLUP_THRESHOLD     — Minimum number of distinct vignes (or vignobles) a cluster
                           must span before promoting to wider scope (default: 2)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Callable

import yaml

from .surrealdb.client import SurrealClient, SurrealError

logger = logging.getLogger("pinard.memory.rollup")

ROLLUP_THRESHOLD = int(os.environ.get("ROLLUP_THRESHOLD", "2"))

# Cosine similarity threshold for embedding-based clustering.
# Items above this threshold are considered semantically overlapping.
CLUSTER_SIMILARITY_THRESHOLD = float(os.environ.get("ROLLUP_CLUSTER_SIMILARITY", "0.75"))

# Maximum items per synthesis cluster (bounds LLM prompt size).
CLUSTER_MAX_SIZE = 12

VIGNOBLE_DB_PREFIX = "vignoble-"
PARCELLE_DB_PREFIX = "parcelle-"
GLOBAL_DB = "__global__"


def _vignoble_db(vignoble_name: str) -> str:
    return f"{VIGNOBLE_DB_PREFIX}{vignoble_name}"


def _parcelle_db(parcelle_name: str) -> str:
    return f"{PARCELLE_DB_PREFIX}{parcelle_name}"


def _load_vignoble_membership(vignes_yaml_path: str) -> dict[str, list[str]]:
    """Parse vignes.yaml and return {vignoble_name: [group_id, ...]}.

    In pinard, each vigne name IS its group_id (the key under `vignes:`).
    All vignes belong to one vignoble (the vignoble that owns the vignes.yaml).
    The vignoble name is derived from the parent directory name of vignes.yaml,
    or from a top-level `name:` key if present.

    Returns a dict with a single entry: {vignoble_name: [vigne1, vigne2, ...]}.
    """
    path = Path(vignes_yaml_path)
    if not path.exists():
        logger.warning("vignes.yaml not found at %s; no vignoble membership", vignes_yaml_path)
        return {}

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    vignoble_name: str = (
        data.get("name")
        or os.environ.get("NATS_VIGNOBLE", "")
        or path.parent.name
    )

    vignes: dict[str, Any] = data.get("vignes") or {}
    group_ids = list(vignes.keys())

    return {vignoble_name: group_ids}


def _get_vignes_yaml_path() -> str:
    explicit = os.environ.get("VIGNOBLE_YAML", "")
    if explicit:
        return explicit
    vignoble_dir = os.environ.get("VIGNOBLE_DIR", ".")
    return str(Path(vignoble_dir) / "vignes.yaml")


def _load_all_vignoble_memberships(vignobles_base_dir: str) -> dict[str, list[str]]:
    """Iterate subdirs of *vignobles_base_dir* and return vignoble memberships.

    Each subdirectory is expected to be a cloned vignoble repo containing a
    ``vignes.yaml``.  The vignoble name is derived from the subdir name
    (``vignoble-<name>`` → ``<name>``; plain names are used as-is).  The
    subdir name is used directly rather than delegating to
    ``_load_vignoble_membership`` to avoid NATS_VIGNOBLE env-var interference.

    Returns:
        ``{vignoble_name: [group_id, ...]}``, one entry per discovered vignoble.
    """
    base = Path(vignobles_base_dir)
    if not base.exists():
        logger.warning("vignobles_base_dir %s does not exist", base)
        return {}

    result: dict[str, list[str]] = {}
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        vignes_yaml = sub / "vignes.yaml"
        if not vignes_yaml.exists():
            continue
        dir_name = sub.name
        vname = dir_name[len("vignoble-"):] if dir_name.startswith("vignoble-") else dir_name
        try:
            with open(vignes_yaml) as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Could not read %s: %s", vignes_yaml, exc)
            continue
        group_ids = list((data.get("vignes") or {}).keys())
        result[vname] = group_ids
    return result


def _get_vignobles_base_dir() -> str | None:
    """Return VIGNOBLES_BASE_DIR if set, else None."""
    return os.environ.get("VIGNOBLES_BASE_DIR", "") or None


def _fetch_all_entities(group_id: str) -> list[dict[str, Any]]:
    """Return all entity records from the SurrealDB database for group_id."""
    try:
        with SurrealClient(group_id=group_id) as surreal:
            results = surreal.query("SELECT * FROM entity")
            if results and results[0]:
                return results[0]
    except SurrealError as exc:
        logger.warning("Could not fetch entities for group %s: %s", group_id, exc)
    return []


def _fetch_all_wiki_docs(group_id: str) -> list[dict[str, Any]]:
    """Return all wiki_doc records (path, title, body, embedding) for group_id."""
    try:
        with SurrealClient(group_id=group_id) as surreal:
            results = surreal.query(
                "SELECT path, title, body, embedding FROM wiki_doc"
            )
            if results and results[0]:
                return results[0]
    except SurrealError as exc:
        logger.warning("Could not fetch wiki_docs for group %s: %s", group_id, exc)
    return []


def _fetch_typed_entities(group_id: str) -> list[dict[str, Any]]:
    """Return typed (non-artifact) entity records for group_id.

    Only Layer 2 typed knowledge (role != 'artifact') is eligible to feed
    curate-on-promote synthesis.  Raw artifacts are excluded entirely.
    """
    try:
        with SurrealClient(group_id=group_id) as surreal:
            results = surreal.query(
                "SELECT role, name, description, embedding FROM entity "
                "WHERE role != 'artifact'"
            )
            if results and results[0]:
                return results[0]
    except SurrealError as exc:
        logger.warning(
            "Could not fetch typed entities for group %s: %s", group_id, exc
        )
    return []


def _cleanup_entity_rows(db_name: str) -> None:
    """Delete promotion-leaked entity rows from a higher-tier scope DB.

    Removes only entity records with provenance='promotion' — rows that leaked
    into vignoble or global scopes via the old entity-promotion path.  Directly
    ingested rows (provenance='engram_pg', 'episode_extraction', or '') are
    preserved so that régisseur/conductor/maître Engram observations stored
    under a vignoble-<name> project survive rollup cycles.

    Best-effort — a failure is logged but does not abort the caller.
    """
    try:
        with SurrealClient(group_id=db_name) as surreal:
            surreal.query("DELETE entity WHERE provenance = 'promotion'")
        logger.info("Cleaned up promotion-leaked entity rows from higher-tier scope: %s", db_name)
    except SurrealError as exc:
        logger.warning(
            "Could not clean up entity rows from scope %s: %s", db_name, exc
        )


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mean_embedding(embeddings: list[list[float]]) -> list[float] | None:
    """Component-wise mean of a list of equal-length embeddings."""
    if not embeddings:
        return None
    dim = len(embeddings[0])
    result = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            result[i] += v
    n = len(embeddings)
    return [v / n for v in result]


def _slugify(text: str) -> str:
    """Convert a name/title to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown"


def _parse_llm_json(raw: str) -> dict[str, str]:
    """Extract {title, summary, body} from an LLM JSON response.

    Tries the full response as JSON first; falls back to finding the first
    embedded JSON object. Returns a (possibly partial) dict.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner.append(line)
        text = "\n".join(inner).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return {k: str(v) for k, v in result.items() if k in ("title", "summary", "body")}
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start: end + 1])
            if isinstance(result, dict):
                return {k: str(v) for k, v in result.items() if k in ("title", "summary", "body")}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ── Item representation ────────────────────────────────────────────────────────

class _KnowledgeItem:
    """A single wiki_doc page or typed entity from a vigne, with its embedding."""

    __slots__ = ("kind", "group_id", "title", "body", "description", "role", "path", "embedding")

    def __init__(
        self,
        kind: str,          # "wiki" | "entity"
        group_id: str,
        title: str,
        embedding: list[float] | None,
        body: str = "",
        description: str = "",
        role: str = "",
        path: str = "",
    ) -> None:
        self.kind = kind
        self.group_id = group_id
        self.title = title
        self.embedding = embedding
        self.body = body
        self.description = description
        self.role = role
        self.path = path

    def to_source_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "group_id": self.group_id,
            "title": self.title,
        }
        if self.kind == "wiki":
            d["body"] = self.body
            d["path"] = self.path
        else:
            d["name"] = self.title
            d["description"] = self.description
        return d


# ── Embedding-similarity clustering ───────────────────────────────────────────

def _cluster_items(
    items: list[_KnowledgeItem],
    embed_fn: Callable[[str], list[float]],
    similarity_threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> list[list[_KnowledgeItem]]:
    """Greedy agglomerative clustering of knowledge items by embedding similarity.

    Items without an embedding (or where the embed call fails) form isolated
    single-item clusters.  Cluster size is capped at CLUSTER_MAX_SIZE.

    Returns a list of clusters; each cluster is a list of _KnowledgeItem.
    """
    if not items:
        return []

    # Compute embeddings for items that don't already have one.
    embeddings: list[list[float] | None] = []
    for item in items:
        if item.embedding and isinstance(item.embedding, list):
            embeddings.append(item.embedding)
        else:
            text = (
                f"{item.title}\n{item.body or item.description}"
            ).strip()
            try:
                embeddings.append(embed_fn(text) if text else None)
            except Exception:
                logger.debug(
                    "Embed failed for item %r (%s) — isolated cluster",
                    item.title, item.group_id,
                )
                embeddings.append(None)

    clusters: list[list[_KnowledgeItem]] = []
    centroids: list[list[float] | None] = []

    for item, emb in zip(items, embeddings):
        if emb is None:
            clusters.append([item])
            centroids.append(None)
            continue

        best_idx: int | None = None
        best_score: float = 0.0

        for idx, centroid in enumerate(centroids):
            if centroid is None:
                continue
            if len(clusters[idx]) >= CLUSTER_MAX_SIZE:
                continue
            score = _cosine_similarity(emb, centroid)
            if score >= similarity_threshold and score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is not None:
            clusters[best_idx].append(item)
            new_centroid = _mean_embedding(
                [c for c in [centroids[best_idx], emb] if c is not None]
            )
            centroids[best_idx] = new_centroid
        else:
            clusters.append([item])
            centroids.append(emb)

    return clusters


def _build_synthesis_candidates(
    clusters: list[list[_KnowledgeItem]],
    threshold: int,
) -> list[dict[str, Any]]:
    """Return clusters spanning >= threshold distinct vignes as synthesis candidates.

    Each candidate is a dict:
        title   — representative title (from the first item in the cluster)
        sources — list of source dicts (to_source_dict() per item)
    """
    candidates = []
    for cluster in clusters:
        vigne_set = {item.group_id for item in cluster}
        if len(vigne_set) < threshold:
            continue
        title = cluster[0].title
        sources = [item.to_source_dict() for item in cluster]
        candidates.append({"title": title, "sources": sources})
    return candidates


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _curate_promoted_wiki(
    db_name: str,
    candidates: list[dict[str, Any]],
    llm_client: Any,
    embed_fn: Callable[[str], list[float]],
) -> int:
    """Synthesise consolidated wiki_doc entries at a higher-tier scope.

    Each candidate is a dict with:
        title   — merged title for the consolidated page
        sources — list of source dicts, each with keys:
                    kind  ("wiki" | "entity")
                    title / name
                    body / description  (wiki: body; entity: description)
                    path (wiki only)
                    group_id

    Returns the number of wiki_docs successfully written.
    """
    written = 0
    for candidate in candidates:
        title = candidate.get("title", "")
        sources = candidate.get("sources", [])
        if not title or not sources:
            continue

        source_lines = []
        for src in sources:
            if src.get("kind") == "wiki":
                source_lines.append(
                    f"- [wiki:{src.get('group_id', '')}] {src.get('title', '')}: "
                    f"{(src.get('body', '') or '')[:300]}"
                )
            else:
                source_lines.append(
                    f"- [{src.get('kind', 'entity')}:{src.get('group_id', '')}] "
                    f"{src.get('name', src.get('title', ''))}: "
                    f"{src.get('description', '')}"
                )
        sources_text = "\n".join(source_lines)

        system = (
            "You are a technical knowledge curator. Synthesize cross-project knowledge "
            "into a single coherent consolidated wiki page. "
            "Respond with a JSON object with exactly three keys: "
            '"title" (one clean human-readable phrase, no trailing colon, no markdown marks), '
            '"summary" (one distilled sentence suitable as a boot-manifest hint), '
            '"body" (a markdown body using sections # Overview, # Details, # Citations). '
            "Output only the JSON object — no other text."
        )
        user_msg = textwrap.dedent(f"""\
            Synthesize a consolidated wiki page for the cross-project concept: **{title}**

            Source pages and entities from multiple projects:
            {sources_text}

            Be concise and factual. Synthesize the knowledge — do not just list items.
            Respond with a JSON object: {{"title": "...", "summary": "...", "body": "..."}}
        """)

        synth_title = title
        synth_summary = ""
        body = ""
        try:
            raw = llm_client.complete(
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=1024,
                system=system,
            )
            parsed = _parse_llm_json(raw)
            synth_title = parsed.get("title") or title
            synth_summary = parsed.get("summary") or ""
            body = parsed.get("body") or ""
        except Exception as exc:
            logger.warning(
                "LLM synthesis failed for promoted wiki '%s' in %s: %s — using fallback",
                title, db_name, exc,
            )

        if not body:
            body = f"# Overview\n\nConsolidated from {len(sources)} source(s).\n"
        if not synth_summary:
            synth_summary = f"Cross-project consolidated knowledge for {synth_title}."

        try:
            embedding = embed_fn(f"{synth_title}\n{body[:500]}") if body else None
        except Exception:
            embedding = None

        slug = _slugify(synth_title)
        try:
            with SurrealClient(group_id=db_name) as surreal:
                surreal.upsert_wiki_doc(
                    title=synth_title,
                    body=body,
                    summary=synth_summary,
                    frontmatter={
                        "type": "consolidated",
                        "source": "rollup-curator",
                        "rollup_scope": db_name,
                        "source_groups": [s.get("group_id", "") for s in sources],
                    },
                    path=f"consolidated/{slug}",
                    confidence=0.75,
                    embedding=embedding,
                )
            written += 1
            logger.info(
                "Curate-on-promote: wrote consolidated wiki_doc '%s' → %s",
                title, db_name,
            )
        except SurrealError as exc:
            logger.warning(
                "Failed to write promoted wiki_doc '%s' in %s: %s", title, db_name, exc
            )

    return written


class ScopeRollupEngine:
    """Aggregates cross-vigne wiki knowledge to vignoble and global scopes.

    Invariant: no entity record (any role — artifact or typed) is ever copied
    to a higher scope.  Only synthesised wiki_doc entries rise.

    Overlap detection uses **embedding-similarity clustering** (cosine ≥
    CLUSTER_SIMILARITY_THRESHOLD) rather than exact string matching, so
    semantically equivalent concepts from different projects are grouped even
    when their paths/names differ.

    Usage::

        engine = ScopeRollupEngine(llm_client=llm, embed_fn=embed)
        engine.run()  # one-shot; call periodically
    """

    def __init__(
        self,
        vignobles_base_dir: str | None = None,
        llm_client: Any = None,
        embed_fn: Callable[[str], list[float]] | None = None,
    ) -> None:
        self._vignobles_base_dir = vignobles_base_dir or _get_vignobles_base_dir()
        self._llm = llm_client
        self._embed = embed_fn

    def run(self) -> dict[str, int]:
        """Execute one full roll-up pass.

        Returns counts: {"vignoble_promoted": 0, "global_promoted": 0,
                          "vignoble_wiki_synthesized": N, "global_wiki_synthesized": M}.
        vignoble_promoted and global_promoted are always 0 (entities never rise).
        """
        membership = _load_all_vignoble_memberships(self._vignobles_base_dir)
        if not membership:
            logger.debug("No vignoble membership found; skipping rollup")
            return {
                "vignoble_promoted": 0,
                "global_promoted": 0,
                "vignoble_wiki_synthesized": 0,
                "global_wiki_synthesized": 0,
            }

        vignoble_wiki_synthesized = 0
        global_wiki_synthesized = 0

        # Track items promoted to vignoble scope, for global roll-up.
        # key: vignoble_name → list of _KnowledgeItem promoted there
        global_items_by_vignoble: dict[str, list[_KnowledgeItem]] = {}

        for vignoble_name, group_ids in membership.items():
            vignoble_db = _vignoble_db(vignoble_name)

            # Step 1: clean up any entity rows that leaked into this vignoble scope.
            _cleanup_entity_rows(vignoble_db)

            if not group_ids:
                continue

            # Step 2: collect all knowledge items (wiki_docs + typed entities) across
            # all group_ids in this vignoble.
            all_items: list[_KnowledgeItem] = []

            for group_id in group_ids:
                for doc in _fetch_all_wiki_docs(group_id):
                    all_items.append(_KnowledgeItem(
                        kind="wiki",
                        group_id=group_id,
                        title=doc.get("title") or doc.get("path", ""),
                        embedding=doc.get("embedding"),
                        body=doc.get("body", ""),
                        path=doc.get("path", ""),
                    ))

                for ent in _fetch_typed_entities(group_id):
                    name = ent.get("name", "")
                    if not name:
                        continue
                    all_items.append(_KnowledgeItem(
                        kind=ent.get("role", "entity"),
                        group_id=group_id,
                        title=name,
                        embedding=ent.get("embedding"),
                        description=ent.get("description", ""),
                        role=ent.get("role", ""),
                    ))

            if not all_items:
                continue

            # Step 3: cluster by embedding similarity; find cross-vigne clusters.
            if not self._embed:
                logger.warning(
                    "Vignoble roll-up: %d item(s) for %s but no embed_fn — "
                    "synthesis skipped (entity promotion will NOT be used as fallback)",
                    len(all_items), vignoble_db,
                )
                continue

            clusters = _cluster_items(all_items, self._embed)
            synthesis_candidates = _build_synthesis_candidates(
                clusters, threshold=ROLLUP_THRESHOLD
            )

            if synthesis_candidates:
                if self._llm:
                    n = _curate_promoted_wiki(
                        vignoble_db, synthesis_candidates, self._llm, self._embed
                    )
                    vignoble_wiki_synthesized += n
                    logger.info(
                        "Vignoble roll-up: synthesised %d wiki_doc(s) → %s",
                        n, vignoble_db,
                    )
                else:
                    logger.warning(
                        "Vignoble roll-up: %d candidate(s) for %s but no LLM — "
                        "synthesis skipped (entity promotion will NOT be used as fallback)",
                        len(synthesis_candidates), vignoble_db,
                    )

            # Collect promoted items for global tier (use one representative per cluster).
            for cluster in clusters:
                vigne_set = {item.group_id for item in cluster}
                if len(vigne_set) < ROLLUP_THRESHOLD:
                    continue
                # Use the first item as the cluster representative for global roll-up.
                global_items_by_vignoble.setdefault(vignoble_name, []).append(cluster[0])

        # Global scope cleanup.
        _cleanup_entity_rows(GLOBAL_DB)

        # Global roll-up: cluster the vignoble-level promoted items across vignobles.
        all_global_items: list[_KnowledgeItem] = []
        for vname, items in global_items_by_vignoble.items():
            for item in items:
                # Re-tag group_id as the vignoble name for cross-vignoble clustering.
                global_items_by_vignoble[vname]  # noqa: B018 (just referencing)
                tagged = _KnowledgeItem(
                    kind=item.kind,
                    group_id=vname,
                    title=item.title,
                    embedding=item.embedding,
                    body=item.body,
                    description=item.description,
                    role=item.role,
                    path=item.path,
                )
                all_global_items.append(tagged)

        if all_global_items and self._embed:
            global_clusters = _cluster_items(all_global_items, self._embed)
            global_candidates = _build_synthesis_candidates(
                global_clusters, threshold=ROLLUP_THRESHOLD
            )
            if global_candidates:
                if self._llm:
                    n = _curate_promoted_wiki(
                        GLOBAL_DB, global_candidates, self._llm, self._embed
                    )
                    global_wiki_synthesized += n
                    logger.info(
                        "Global roll-up: synthesised %d wiki_doc(s) → %s",
                        n, GLOBAL_DB,
                    )
                else:
                    logger.warning(
                        "Global roll-up: %d candidate(s) for __global__ but no LLM — "
                        "synthesis skipped (entity promotion will NOT be used as fallback)",
                        len(global_candidates),
                    )
        elif all_global_items and not self._embed:
            logger.warning(
                "Global roll-up: items available for __global__ but no embed_fn — skipping"
            )

        logger.info(
            "Roll-up complete: vignoble_wiki_synthesized=%d global_wiki_synthesized=%d",
            vignoble_wiki_synthesized, global_wiki_synthesized,
        )
        return {
            "vignoble_promoted": 0,
            "global_promoted": 0,
            "vignoble_wiki_synthesized": vignoble_wiki_synthesized,
            "global_wiki_synthesized": global_wiki_synthesized,
        }
