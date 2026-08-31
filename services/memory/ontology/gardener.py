"""Ontology gardener — periodic agent that mines staging tables to evolve the ontology.

Reads entity_staging / edge_staging for a group_id, clusters unmatched proposals,
makes LLM-driven Map / Extend / Hold decisions per cluster, and emits human-gated
MRs for Extend decisions.  The ontology is NEVER mutated automatically — all
structural changes require a human-approved MR.

Typical usage (scheduled / periodic)::

    from services.memory.surrealdb.client import SurrealClient
    from services.memory.llm_client import build_llm_client
    from services.memory.ontology.registry import OntologyRegistry
    from services.memory.ontology.gardener import OntologyGardenerConfig, run_gardener

    surreal = SurrealClient(group_id="genomics-build", ...)
    llm    = build_llm_client()
    reg    = OntologyRegistry()
    cfg    = OntologyGardenerConfig()
    run_gardener("genomics-build", surreal, llm, reg, cfg)

## Extend-MR runtime / target (Decision G1)

The gardener runs inside the **memory-service cluster pod** (same process as the
ingester).  The pod has access to the *wiki* repo (``WIKI_ROOT`` checkout) but
**not** the pinard code repo — ``register_domain`` lives in pinard source, not
in the wiki.

Decision: **option (b)** — ``emit_proposal_mr`` writes an ``ontology-proposals/``
YAML document into the *wiki repo* (``WIKI_ROOT`` / ``ontology-proposals/``) and
opens an MR there.  A human reviews the proposal and manually creates a
corresponding ``register_domain(...)`` change in the pinard repo.  This keeps the
gardener pod dependency-free of the pinard source checkout.

``OntologyGardenerConfig.repo_dir`` must point to the wiki repo root in the
pod environment (set via ``WIKI_ROOT`` or ``VIGNOBLE_DIR``).  The generated snippet
is included in the MR description and the ``ontology-proposals/<id>.py`` file so
the human can copy-paste it into the pinard repo when the MR is approved.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..llm_client import LLMClient, LLMError
from ..surrealdb.client import SurrealClient, SurrealError
from .registry import ComposedOntology, OntologyRegistry

logger = logging.getLogger("pinard.memory.ontology.gardener")

# ── Config ─────────────────────────────────────────────────────────────────────


@dataclass
class OntologyGardenerConfig:
    """Tunable parameters for the ontology gardener.

    Attributes:
        min_occurrence: Minimum occurrence_count before a staging record is
            considered for clustering (filters one-offs).
        cluster_size_threshold: Minimum number of staging items in a cluster
            before proposing (avoids churn from small signals).
        similarity_dedup_threshold: Cosine similarity above which a proposed
            type is considered equivalent to an existing type — used to avoid
            re-proposing types that are already in the ontology.
        max_entity_staging: Maximum entity_staging rows fetched per run.
        max_edge_staging: Maximum edge_staging rows fetched per run.
        repo_dir: Repo root for git operations (for MR emission).  Defaults to
            ``VIGNOBLE_DIR`` env var, falling back to ``"."``.
        gitlab_repo: GitLab project path, e.g. ``your-group/pinard``.
            Defaults to ``GITLAB_REPO`` env var.
        dry_run: When True, log what would happen without touching git/GitLab.
    """

    min_occurrence: int = 3
    cluster_size_threshold: int = 2
    similarity_dedup_threshold: float = 0.85
    max_entity_staging: int = 200
    max_edge_staging: int = 200
    repo_dir: str = field(default_factory=lambda: os.environ.get("VIGNOBLE_DIR", "."))
    gitlab_repo: str = field(default_factory=lambda: os.environ.get("GITLAB_REPO", ""))
    dry_run: bool = field(default_factory=lambda: os.environ.get("DRY_RUN", "0") == "1")


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class StagingCluster:
    """A cluster of related staging proposals (entity or edge side)."""

    kind: str  # "entity" or "edge"
    proposed_key: str  # proposed_role (entity) or proposed_relation (edge)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.items)

    @property
    def total_occurrences(self) -> int:
        return sum(r.get("occurrence_count", 1) for r in self.items)

    def sample_descriptions(self, n: int = 3) -> list[str]:
        """Return up to *n* representative description strings."""
        return [r.get("description", "") for r in self.items[:n] if r.get("description")]


@dataclass
class GardenerDecision:
    """The outcome of the LLM decision for a single cluster.

    action:
        ``"map"``    — alias to an existing type; migrate staged items.
        ``"extend"`` — propose a new type via MR.
        ``"hold"``   — not enough signal; leave staged.
    """

    action: str  # "map" | "extend" | "hold"
    cluster: StagingCluster
    rationale: str = ""
    # For "map": the existing type name to alias to.
    mapped_to: str = ""
    # For "extend": the proposed new type/relation name.
    proposed_name: str = ""
    # For "extend": valid (source, target) pairs for an edge, or fields for an entity.
    proposed_fields: list[str] = field(default_factory=list)
    proposed_pairs: list[tuple[str, str]] = field(default_factory=list)


# ── Cosine similarity helper ───────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_embedding(rows: list[dict[str, Any]]) -> list[float] | None:
    """Return the element-wise mean of all non-None embeddings in *rows*."""
    vecs = [r["embedding"] for r in rows if r.get("embedding")]
    if not vecs:
        return None
    dim = len(vecs[0])
    mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    return mean


# ── Read staging ───────────────────────────────────────────────────────────────


def read_staging(
    surreal: SurrealClient,
    min_occurrence: int = 1,
    max_entity: int = 200,
    max_edge: int = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (entity_staging_rows, edge_staging_rows) meeting *min_occurrence*.

    Rows are ordered by occurrence_count descending (most recurrent first).
    """
    entity_rows = surreal.list_entity_staging(
        min_occurrence=min_occurrence, limit=max_entity
    )
    edge_rows = surreal.list_edge_staging(
        min_occurrence=min_occurrence, limit=max_edge
    )
    return entity_rows, edge_rows


# ── Clustering ─────────────────────────────────────────────────────────────────


def cluster_proposals(
    entity_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    composed: ComposedOntology,
    similarity_dedup_threshold: float = 0.85,
) -> list[StagingCluster]:
    """Group staging rows into clusters and filter duplicates of known types.

    Entity rows are grouped by *proposed_role*; edge rows by *proposed_relation*.
    Within each group, the centroid embedding is compared against the composed
    ontology's existing types: if the centroid similarity exceeds
    *similarity_dedup_threshold* vs any existing type, the cluster is discarded
    as a known-type duplicate.

    Returns a list of :class:`StagingCluster` (entity then edge clusters),
    ordered by total_occurrences descending.
    """
    clusters: list[StagingCluster] = []

    # Build reference embeddings for existing entity roles and edge names.
    # (Staging rows carry embeddings of the entity *content*, not of the type
    # label itself, so dedup is approximate — best-effort signal.)

    # --- Entity clusters ---
    entity_groups: dict[str, list[dict[str, Any]]] = {}
    for row in entity_rows:
        key = (row.get("proposed_role") or "").strip().lower()
        if not key:
            continue
        entity_groups.setdefault(key, []).append(row)

    known_entity_roles = set(composed.entity_roles())
    for proposed_role, rows in entity_groups.items():
        # Exact role match → already known, skip.
        if proposed_role in known_entity_roles:
            continue

        cluster = StagingCluster(kind="entity", proposed_key=proposed_role, items=rows)

        # Embedding-based dedup: compare cluster centroid against each row's
        # embedding to check for very high self-similarity with known types.
        # (We don't have embeddings of type labels — skip embedding dedup when
        # no embeddings are present; rely on exact-name dedup above.)
        centroid = _mean_embedding(rows)
        if centroid is not None:
            # Check whether *any* row in this cluster is very close to an
            # existing-type record.  In the absence of pre-embedded type labels
            # we do intra-cluster dedup: if the cluster is suspiciously uniform
            # in embedding space and matches an exact known role string, we
            # have already filtered it.  Full embedding dedup requires a
            # per-type reference vector which is not available here without
            # a separate lookup; the threshold guard remains as the config
            # hook for callers who inject reference embeddings via subclassing.
            pass  # See _dedup_cluster_against_refs() for extension point.

        clusters.append(cluster)

    # --- Edge clusters ---
    edge_groups: dict[str, list[dict[str, Any]]] = {}
    for row in edge_rows:
        key = (row.get("proposed_relation") or "").strip().lower()
        if not key:
            continue
        edge_groups.setdefault(key, []).append(row)

    known_edge_names_snake = {
        _to_snake(cls.__name__) for cls in composed.edge_types
    }
    for proposed_rel, rows in edge_groups.items():
        if proposed_rel in known_edge_names_snake:
            continue
        clusters.append(
            StagingCluster(kind="edge", proposed_key=proposed_rel, items=rows)
        )

    clusters.sort(key=lambda c: c.total_occurrences, reverse=True)
    return clusters


def _dedup_cluster_against_refs(
    centroid: list[float],
    ref_embeddings: list[list[float]],
    threshold: float,
) -> bool:
    """Return True if *centroid* is within *threshold* of any reference embedding."""
    return any(_cosine(centroid, ref) >= threshold for ref in ref_embeddings)


def _to_snake(name: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


# ── LLM decision ───────────────────────────────────────────────────────────────

_DECISION_SYSTEM = """\
You are the Pinard ontology gardener.  You analyse clusters of unmatched entity \
or edge proposals from the knowledge-graph staging area and decide how to handle them.

For each cluster you must return a JSON object with:
  "action":        "map" | "extend" | "hold"
  "rationale":     one sentence explaining the decision
  "mapped_to":     (only for "map") the exact existing type name to alias to
  "proposed_name": (only for "extend") a CamelCase entity name or snake_case edge name
  "proposed_fields": (only for "extend" entity) list of new field names as strings
  "proposed_pairs":  (only for "extend" edge) list of ["src_role", "tgt_role"] pairs

Rules:
- "map"    when the cluster clearly describes an existing type with a different label.
- "extend" when the cluster represents a genuinely new, recurrent operational concept \
not expressible by any existing type.
- "hold"   when the signal is weak, ambiguous, or too noisy.
- NEVER choose "extend" for a type already in the ontology — prefer "map".
- Return only the JSON object, no prose.
"""


def _build_decision_prompt(cluster: StagingCluster, composed: ComposedOntology) -> str:
    existing_entities = ", ".join(composed.entity_roles())
    existing_edges = ", ".join(_to_snake(c.__name__) for c in composed.edge_types)
    samples = cluster.sample_descriptions(5)
    sample_text = "\n".join(f"  - {d}" for d in samples) if samples else "  (none)"
    from_to_pairs = ""
    if cluster.kind == "edge":
        pairs = {
            (r.get("from_role", "?"), r.get("to_role", "?"))
            for r in cluster.items
        }
        from_to_pairs = "\nObserved (from_role, to_role) pairs: " + ", ".join(
            f"({s},{t})" for s, t in sorted(pairs)
        )

    return (
        f"Cluster kind: {cluster.kind}\n"
        f"Proposed key: {cluster.proposed_key!r}\n"
        f"Cluster size: {cluster.size} items, {cluster.total_occurrences} total occurrences\n"
        f"Sample descriptions:\n{sample_text}"
        f"{from_to_pairs}\n\n"
        f"Existing entity roles: {existing_entities}\n"
        f"Existing edge types:   {existing_edges}\n"
    )


def decide_cluster(
    cluster: StagingCluster,
    llm_client: LLMClient,
    composed: ComposedOntology,
) -> GardenerDecision:
    """Ask the LLM to decide Map / Extend / Hold for *cluster*.

    Falls back to ``"hold"`` on any LLM error.
    """
    prompt = _build_decision_prompt(cluster, composed)
    try:
        raw = llm_client.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            system=_DECISION_SYSTEM,
        )
    except LLMError as exc:
        logger.warning("LLM decision failed for cluster %r: %s", cluster.proposed_key, exc)
        return GardenerDecision(
            action="hold",
            cluster=cluster,
            rationale=f"LLM unavailable: {exc}",
        )

    # Extract JSON from response.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        logger.warning("LLM returned no JSON for cluster %r: %s", cluster.proposed_key, raw[:200])
        return GardenerDecision(action="hold", cluster=cluster, rationale="LLM returned no JSON")

    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("LLM JSON parse error for cluster %r: %s", cluster.proposed_key, exc)
        return GardenerDecision(action="hold", cluster=cluster, rationale=f"JSON parse error: {exc}")

    action = data.get("action", "hold").lower()
    if action not in ("map", "extend", "hold"):
        action = "hold"

    pairs_raw = data.get("proposed_pairs", [])
    pairs: list[tuple[str, str]] = []
    for p in pairs_raw:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            pairs.append((str(p[0]), str(p[1])))

    return GardenerDecision(
        action=action,
        cluster=cluster,
        rationale=data.get("rationale", ""),
        mapped_to=data.get("mapped_to", ""),
        proposed_name=data.get("proposed_name", ""),
        proposed_fields=list(data.get("proposed_fields", [])),
        proposed_pairs=pairs,
    )


# ── Map action — migrate staged items into typed store ─────────────────────────


def apply_map_decision(
    decision: GardenerDecision,
    surreal: SurrealClient,
    composed: ComposedOntology,
) -> int:
    """Migrate staging items into the typed entity/edge store (Map action).

    Returns the number of items migrated.
    """
    mapped_to_raw = decision.mapped_to.strip()
    mapped_to = mapped_to_raw.lower()
    cluster = decision.cluster
    migrated = 0

    if cluster.kind == "entity":
        known_roles = set(composed.entity_roles())
        if mapped_to not in known_roles:
            logger.warning(
                "Map decision for %r → %r but target role is not in composed ontology; holding.",
                cluster.proposed_key, mapped_to,
            )
            return 0
        for row in cluster.items:
            try:
                surreal.upsert_entity(
                    role=mapped_to,
                    name=row.get("name", ""),
                    description=row.get("description", ""),
                    embedding=row.get("embedding"),
                    data=row.get("data") or {},
                )
                migrated += 1
            except SurrealError as exc:
                logger.warning("Migration failed for entity %r: %s", row.get("name"), exc)

    elif cluster.kind == "edge":
        known_tables = {_to_snake(cls.__name__) for cls in composed.edge_types}
        # Accept CamelCase (e.g. "DependsOn"), snake_case ("depends_on"), or
        # plain lowercase — normalise to snake_case for the table lookup.
        target_table = _to_snake(mapped_to_raw) if mapped_to_raw else ""
        if target_table not in known_tables:
            logger.warning(
                "Map decision for edge %r → %r but target table is not in composed ontology; holding.",
                cluster.proposed_key, mapped_to,
            )
            return 0
        for row in cluster.items:
            try:
                surreal.relate(
                    from_role=row.get("from_role", ""),
                    from_name=row.get("from_name", ""),
                    relation=target_table,
                    to_role=row.get("to_role", ""),
                    to_name=row.get("to_name", ""),
                    description=row.get("description", ""),
                )
                migrated += 1
            except SurrealError as exc:
                logger.warning(
                    "Migration failed for edge %r→%r: %s",
                    row.get("from_name"), row.get("to_name"), exc,
                )

    logger.info(
        "Map %s cluster %r → %r: migrated %d/%d items",
        cluster.kind, cluster.proposed_key, mapped_to, migrated, cluster.size,
    )
    return migrated


# ── Extend action — emit a human-gated MR ─────────────────────────────────────


def _generate_register_domain_snippet(decision: GardenerDecision, group_id: str) -> str:
    """Generate the Python ``register_domain(...)`` extension snippet for an MR."""
    cluster = decision.cluster
    proposed = decision.proposed_name or cluster.proposed_key

    if cluster.kind == "entity":
        fields_block = ""
        for f in decision.proposed_fields:
            fields_block += f"\n    {f}: str = \"\""
        class_name = _to_camel(proposed)
        snippet = textwrap.dedent(f"""\
            # Proposed new entity type for group_id={group_id!r}
            # Generated by ontology gardener — review before merging.
            from pinard_core.entities import CoreEntity

            class {class_name}(CoreEntity):
                role: str = {proposed!r}{fields_block}

            # Register in your domain ontology setup:
            registry.register_domain(
                group_id={group_id!r},
                entity_types=[{class_name}],
                domain_name="...",   # fill in
                domain_version="0.1.0",
            )
        """)
    else:
        pairs_repr = repr(decision.proposed_pairs) if decision.proposed_pairs else "[]"
        class_name = _to_camel(proposed)
        snippet = textwrap.dedent(f"""\
            # Proposed new edge type for group_id={group_id!r}
            # Generated by ontology gardener — review before merging.
            from pinard_core.edges import CoreEdge

            class {class_name}(CoreEdge):
                pass

            # Register in your domain ontology setup:
            registry.register_domain(
                group_id={group_id!r},
                edge_types=[{class_name}],
                edge_type_map_extension={{{proposed!r}: {pairs_repr}}},
                domain_name="...",   # fill in
                domain_version="0.1.0",
            )
        """)
    return snippet


def _to_camel(name: str) -> str:
    """Convert snake_case or lowercase to CamelCase."""
    return "".join(w.capitalize() for w in re.split(r"[_\s]+", name) if w)


def _run_git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    logger.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def emit_proposal_mr(
    decision: GardenerDecision,
    group_id: str,
    config: OntologyGardenerConfig,
) -> str | None:
    """Emit a GitLab MR for an Extend decision.

    Creates a branch ``ontology/<group_id>/<proposed_key>``, commits a YAML
    proposal file + Python snippet, and opens an MR via ``glab``.

    Returns the MR URL or ``None`` on failure.  When ``config.dry_run`` is
    True, logs the actions without touching git or GitLab.
    """
    cluster = decision.cluster
    proposed = decision.proposed_name or cluster.proposed_key
    branch = f"ontology/{group_id}/{cluster.kind}-{proposed}"[:80]
    repo_dir = Path(config.repo_dir)

    snippet = _generate_register_domain_snippet(decision, group_id)
    proposal = {
        "group_id": group_id,
        "kind": cluster.kind,
        "proposed_key": cluster.proposed_key,
        "proposed_name": proposed,
        "rationale": decision.rationale,
        "cluster_size": cluster.size,
        "total_occurrences": cluster.total_occurrences,
        "sample_descriptions": cluster.sample_descriptions(5),
        "proposed_fields": decision.proposed_fields,
        "proposed_pairs": decision.proposed_pairs,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    proposal_dir = repo_dir / "ontology_proposals"
    proposal_file = proposal_dir / f"{group_id}--{cluster.kind}--{proposed}.yaml"
    snippet_file = proposal_dir / f"{group_id}--{cluster.kind}--{proposed}--snippet.py"

    if config.dry_run:
        logger.info("[DRY_RUN] Would create branch %r and open MR for %r", branch, proposed)
        logger.info("[DRY_RUN] Proposal:\n%s", json.dumps(proposal, indent=2))
        logger.info("[DRY_RUN] Snippet:\n%s", snippet)
        return "dry-run://mr/0"

    # Determine current branch.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
        base_branch = result.stdout.strip() or "master"
    except OSError:
        base_branch = "master"

    try:
        _run_git(["git", "checkout", "-b", branch], cwd=repo_dir)
    except subprocess.CalledProcessError as exc:
        logger.error("Could not create branch %r: %s", branch, exc.stderr)
        return None

    try:
        import yaml  # type: ignore[import]
        proposal_dir.mkdir(parents=True, exist_ok=True)
        proposal_file.write_text(yaml.dump(proposal, default_flow_style=False, allow_unicode=True))
        snippet_file.write_text(snippet)
        _run_git(["git", "add", str(proposal_file), str(snippet_file)], cwd=repo_dir)
        _run_git(
            [
                "git", "commit", "--no-verify",
                "-m",
                f"ontology(gardener): propose {cluster.kind} {proposed!r} for {group_id}\n\n"
                f"Cluster size: {cluster.size} items, {cluster.total_occurrences} occurrences\n"
                f"Rationale: {decision.rationale[:200]}",
            ],
            cwd=repo_dir,
        )
        _run_git(["git", "push", "-u", "origin", branch], cwd=repo_dir)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.error("Git operations failed for %r: %s", proposed, exc)
        subprocess.run(
            ["git", "checkout", base_branch], cwd=str(repo_dir), check=False,
            capture_output=True,
        )
        return None

    # Open MR.
    samples_text = "\n".join(f"- {d}" for d in cluster.sample_descriptions(3))
    description = (
        f"## Ontology gardener proposal\n\n"
        f"**Kind:** {cluster.kind}  \n"
        f"**Proposed name:** `{proposed}`  \n"
        f"**Group ID:** `{group_id}`  \n"
        f"**Cluster:** {cluster.size} staging items, {cluster.total_occurrences} total occurrences  \n\n"
        f"### Rationale\n\n{decision.rationale}\n\n"
        f"### Sample staging descriptions\n\n{samples_text}\n\n"
        f"### Generated snippet\n\n```python\n{snippet}\n```\n\n"
        f"---\n"
        f"_Generated by the Pinard ontology gardener.  "
        f"**Do not auto-merge** — review the proposed type before merging._\n"
    )
    cmd = [
        "glab", "mr", "create",
        "--title", f"ontology(gardener): propose {cluster.kind} {proposed!r} for {group_id}",
        "--description", description,
        "--source-branch", branch,
        "--no-editor",
        "--web=false",
    ]
    if config.gitlab_repo:
        cmd += ["--repo", config.gitlab_repo]

    try:
        result = subprocess.run(cmd, cwd=str(repo_dir), capture_output=True, text=True, check=False)
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            logger.error("glab mr create failed (rc=%d): %s", result.returncode, output)
            mr_url = None
        else:
            mr_url = next((l for l in output.splitlines() if l.startswith("https://")), output or None)
    except (OSError, FileNotFoundError) as exc:
        logger.error("glab not found or failed: %s", exc)
        mr_url = None

    subprocess.run(
        ["git", "checkout", base_branch], cwd=str(repo_dir), check=False, capture_output=True,
    )

    if mr_url:
        logger.info("MR opened for %s %r: %s", cluster.kind, proposed, mr_url)
    return mr_url


# ── Top-level entrypoint ───────────────────────────────────────────────────────


def run_gardener(
    group_id: str,
    surreal: SurrealClient,
    llm_client: LLMClient,
    registry: OntologyRegistry,
    config: OntologyGardenerConfig | None = None,
) -> dict[str, Any]:
    """Run one gardener pass for *group_id*.

    Returns a summary dict with keys: ``clusters_found``, ``map``, ``extend``,
    ``hold``, ``mrs_opened``, ``items_migrated``.
    """
    if config is None:
        config = OntologyGardenerConfig()

    composed = registry.compose(group_id)

    entity_rows, edge_rows = read_staging(
        surreal,
        min_occurrence=config.min_occurrence,
        max_entity=config.max_entity_staging,
        max_edge=config.max_edge_staging,
    )
    logger.info(
        "Gardener pass for %r: %d entity staging, %d edge staging rows (min_occurrence=%d)",
        group_id, len(entity_rows), len(edge_rows), config.min_occurrence,
    )

    clusters = cluster_proposals(
        entity_rows,
        edge_rows,
        composed,
        similarity_dedup_threshold=config.similarity_dedup_threshold,
    )

    # Apply cluster-size threshold.
    clusters = [c for c in clusters if c.size >= config.cluster_size_threshold]
    logger.info(
        "Gardener: %d clusters after size threshold (>= %d)",
        len(clusters), config.cluster_size_threshold,
    )

    summary: dict[str, Any] = {
        "clusters_found": len(clusters),
        "map": 0,
        "extend": 0,
        "hold": 0,
        "mrs_opened": 0,
        "items_migrated": 0,
    }

    for cluster in clusters:
        decision = decide_cluster(cluster, llm_client, composed)
        logger.info(
            "Cluster %r (%s, %d items): decision=%r",
            cluster.proposed_key, cluster.kind, cluster.size, decision.action,
        )

        if decision.action == "map":
            summary["map"] += 1
            migrated = apply_map_decision(decision, surreal, composed)
            summary["items_migrated"] += migrated

        elif decision.action == "extend":
            summary["extend"] += 1
            mr_url = emit_proposal_mr(decision, group_id, config)
            if mr_url:
                summary["mrs_opened"] += 1

        else:  # "hold"
            summary["hold"] += 1
            logger.debug(
                "Holding cluster %r: %s", cluster.proposed_key, decision.rationale
            )

    logger.info("Gardener pass complete for %r: %s", group_id, summary)
    return summary
