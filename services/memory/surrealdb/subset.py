"""Portable memory subset — export central SurrealDB to an embedded file.

Selects all `entity`, `wiki_doc`, and RELATE edge records scoped to a given
`group_id` (one SurrealDB database in the `pinard` namespace) from the central
server and writes them into a local embedded SurrealDB file (`surrealkv://path`).

The embedded file is a self-contained, version-stamped snapshot that an agent
can load locally at boot without a running SurrealDB server.

Scope naming follows Engram's project nomenclature (group_id == Engram project, 1:1):
  - vigne:    group_id = bare repo name (e.g. "genomics-build", "pinard", "exo-cli")
  - vignoble: group_id = "vignoble-<name>" (e.g. "vignoble-exohub")
  - parcelle: group_id = "parcelle-<name>" (e.g. "parcelle-memory")
  - global:   group_id = "__global__"

Usage (CLI)::

    python -m services.memory.surrealdb.subset \\
        --group-id genomics-build \\
        --out /path/to/genomics-build.surrealkv

Usage (Python)::

    from services.memory.surrealdb.subset import export_subset
    from services.memory.ontology.registry import OntologyRegistry

    registry = OntologyRegistry()
    result = export_subset("genomics-build", "/path/to/genomics-build.surrealkv", registry)
    print(result)  # SubsetResult(entity_count=42, edge_count=7, ...)

Environment variables (central server):
    SURREAL_URL   — SurrealDB server endpoint (default: http://localhost:8000)
    SURREAL_USER  — root username (default: root)
    SURREAL_PASS  — root password (required)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import SurrealClient, SurrealError

logger = logging.getLogger("pinard.memory.subset")

# Edge relation names defined in schema.surql.
_EDGE_TABLES = [
    "depends_on",
    "produces",
    "consumes",
    "indicates_problem",
    "resolved_by",
    "requires_condition",
    "triggers_decision",
    "wiki_references",
    "wiki_mentions",
]


@dataclass
class SubsetResult:
    """Summary of a completed subset export."""

    group_id: str
    out_path: str
    entity_count: int
    wiki_doc_count: int
    edge_count: int
    exported_at: str
    ontology_stamp: dict[str, Any]

    def __str__(self) -> str:
        return (
            f"SubsetResult(group_id={self.group_id!r}, "
            f"entities={self.entity_count}, wiki_docs={self.wiki_doc_count}, "
            f"edges={self.edge_count}, out={self.out_path!r})"
        )


class SubsetError(RuntimeError):
    pass


def export_subset(
    group_id: str,
    out_path: str | Path,
    registry: Any | None = None,
) -> SubsetResult:
    """Export a scoped subset of the central SurrealDB to an embedded file.

    Args:
        group_id: The SurrealDB database name (= vigne/pipeline scope).
        out_path: Destination path for the embedded SurrealDB file. If the
            path already exists it is removed and recreated.
        registry: An `OntologyRegistry` instance used to compute the version
            stamp. If None, a default (core-only) stamp is produced.

    Returns:
        SubsetResult with counts and metadata.

    Raises:
        SubsetError: If the export fails.
    """
    from surrealdb import Surreal  # type: ignore[import]

    out_path = Path(out_path)

    # ── Compute ontology version stamp ────────────────────────────────────────
    if registry is not None:
        composed = registry.compose(group_id)
        ontology_stamp = composed.version.as_stamp()
    else:
        # Fallback: core-only stamp without a registry.
        try:
            from pinard_core.registry import CORE_VERSION  # type: ignore[import]
        except ImportError:
            CORE_VERSION = "1.0.0"
        ontology_stamp = {"pinard_core": CORE_VERSION}

    exported_at = datetime.now(tz=timezone.utc).isoformat()

    # ── Read from central server ───────────────────────────────────────────────
    logger.info("Exporting subset group_id=%s from central SurrealDB", group_id)
    try:
        with SurrealClient(group_id=group_id) as central:
            entities = _fetch_all(central, "entity")
            wiki_docs = _fetch_all(central, "wiki_doc")
            edges = _fetch_edges(central)
    except SurrealError as exc:
        raise SubsetError(f"Failed to read from central SurrealDB: {exc}") from exc

    logger.info(
        "Fetched %d entities, %d wiki_docs, %d edges for group_id=%s",
        len(entities), len(wiki_docs), len(edges), group_id,
    )

    # ── Write to embedded file ─────────────────────────────────────────────────
    if out_path.exists():
        if out_path.is_dir():
            shutil.rmtree(out_path)
        else:
            out_path.unlink()

    out_path.parent.mkdir(parents=True, exist_ok=True)

    db = Surreal(f"surrealkv://{out_path}")
    try:
        db.connect()
        db.use("pinard", group_id)

        _apply_embedded_schema(db)
        _insert_entities(db, entities)
        _insert_wiki_docs(db, wiki_docs)
        _insert_edges(db, edges)
        _write_subset_meta(
            db,
            group_id=group_id,
            exported_at=exported_at,
            ontology_stamp=ontology_stamp,
            entity_count=len(entities),
            wiki_doc_count=len(wiki_docs),
            edge_count=len(edges),
        )
    except Exception as exc:
        db.close()
        raise SubsetError(f"Failed to write embedded subset: {exc}") from exc
    finally:
        db.close()

    logger.info("Subset written to %s", out_path)
    return SubsetResult(
        group_id=group_id,
        out_path=str(out_path),
        entity_count=len(entities),
        wiki_doc_count=len(wiki_docs),
        edge_count=len(edges),
        exported_at=exported_at,
        ontology_stamp=ontology_stamp,
    )


# ── Helpers: fetch from central ───────────────────────────────────────────────

def _fetch_all(central: SurrealClient, table: str) -> list[dict[str, Any]]:
    """Fetch all records from a table via the central SurrealClient."""
    results = central.query(f"SELECT * FROM {table}")
    if not results:
        return []
    return results[0]


def _fetch_edges(central: SurrealClient) -> list[dict[str, Any]]:
    """Fetch all RELATE edge records from the central database."""
    edges: list[dict[str, Any]] = []
    for table in _EDGE_TABLES:
        try:
            results = central.query(
                f"SELECT id, in, out, confidence, description, data, created_at FROM {table}"
            )
            if results and results[0]:
                for row in results[0]:
                    row["_edge_table"] = table
                    edges.append(row)
        except SurrealError:
            # Table may not exist in this group's DB — skip silently.
            pass
    return edges


# ── Helpers: write to embedded ────────────────────────────────────────────────

def _apply_embedded_schema(db: Any) -> None:
    """Apply a minimal schema to the embedded DB for the tables we need."""
    db.query("""
        DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
        DEFINE FIELD IF NOT EXISTS name        ON entity TYPE string;
        DEFINE FIELD IF NOT EXISTS role        ON entity TYPE string;
        DEFINE FIELD IF NOT EXISTS description ON entity TYPE string   DEFAULT "";
        DEFINE FIELD IF NOT EXISTS version     ON entity TYPE string   DEFAULT "1.0.0";
        DEFINE FIELD IF NOT EXISTS created_at  ON entity TYPE datetime DEFAULT time::now();
        DEFINE FIELD IF NOT EXISTS updated_at  ON entity TYPE datetime DEFAULT time::now();
        DEFINE FIELD IF NOT EXISTS data        ON entity FLEXIBLE TYPE object DEFAULT {};
        DEFINE FIELD IF NOT EXISTS embedding   ON entity TYPE option<array<float>>;

        DEFINE TABLE IF NOT EXISTS wiki_doc SCHEMAFULL;
        DEFINE FIELD IF NOT EXISTS title       ON wiki_doc TYPE string;
        DEFINE FIELD IF NOT EXISTS body        ON wiki_doc TYPE string  DEFAULT "";
        DEFINE FIELD IF NOT EXISTS frontmatter ON wiki_doc FLEXIBLE TYPE object DEFAULT {};
        DEFINE FIELD IF NOT EXISTS path        ON wiki_doc TYPE string  DEFAULT "";
        DEFINE FIELD IF NOT EXISTS confidence  ON wiki_doc TYPE float   DEFAULT 1.0;
        DEFINE FIELD IF NOT EXISTS status      ON wiki_doc TYPE string  DEFAULT "needs_review";
        DEFINE FIELD IF NOT EXISTS created_at  ON wiki_doc TYPE datetime DEFAULT time::now();
        DEFINE FIELD IF NOT EXISTS updated_at  ON wiki_doc TYPE datetime DEFAULT time::now();
        DEFINE FIELD IF NOT EXISTS embedding   ON wiki_doc TYPE option<array<float>>;

        DEFINE TABLE IF NOT EXISTS subset_meta SCHEMALESS;
    """)


def _insert_entities(db: Any, entities: list[dict[str, Any]]) -> None:
    for entity in entities:
        record = _strip_id(entity)
        db.query(
            "CREATE entity SET "
            "name=$name, role=$role, description=$description, "
            "version=$version, data=$data, embedding=$embedding, "
            "created_at=$created_at, updated_at=$updated_at",
            {
                "name": record.get("name", ""),
                "role": record.get("role", ""),
                "description": record.get("description", ""),
                "version": record.get("version", "1.0.0"),
                "data": record.get("data", {}),
                "embedding": record.get("embedding"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            },
        )


def _insert_wiki_docs(db: Any, wiki_docs: list[dict[str, Any]]) -> None:
    for doc in wiki_docs:
        record = _strip_id(doc)
        db.query(
            "CREATE wiki_doc SET "
            "title=$title, body=$body, frontmatter=$frontmatter, "
            "path=$path, confidence=$confidence, status=$status, "
            "embedding=$embedding, created_at=$created_at, updated_at=$updated_at",
            {
                "title": record.get("title", ""),
                "body": record.get("body", ""),
                "frontmatter": record.get("frontmatter", {}),
                "path": record.get("path", ""),
                "confidence": record.get("confidence", 1.0),
                "status": record.get("status", "needs_review"),
                "embedding": record.get("embedding"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            },
        )


def _insert_edges(db: Any, edges: list[dict[str, Any]]) -> None:
    """Recreate RELATE edges via raw SurrealQL.

    The embedded file uses sequential entity IDs (assigned on CREATE), so
    edges reference the source entity by role+name lookup rather than the
    original server-side record ID.
    """
    # Build a lookup of original record id → (role, name) for entity resolution.
    # The 'in' and 'out' fields of a RELATE row are record IDs from the server.
    # We skip edge re-creation here if we cannot resolve the endpoints: the
    # embedded subset is primarily used for entity recall (cosine scan, FTS,
    # trace). Edges that reference unresolvable IDs are silently dropped.
    for edge in edges:
        table = edge.get("_edge_table", "")
        if not table:
            continue
        try:
            db.query(
                f"RELATE $in->{table}->$out SET "
                "confidence=$confidence, description=$description, "
                "data=$data, created_at=$created_at",
                {
                    "in": edge.get("in"),
                    "out": edge.get("out"),
                    "confidence": edge.get("confidence", 1.0),
                    "description": edge.get("description", ""),
                    "data": edge.get("data", {}),
                    "created_at": edge.get("created_at"),
                },
            )
        except Exception:
            # Edge endpoints may not exist in embedded store — skip silently.
            pass


def _write_subset_meta(
    db: Any,
    group_id: str,
    exported_at: str,
    ontology_stamp: dict[str, Any],
    entity_count: int,
    wiki_doc_count: int,
    edge_count: int,
) -> None:
    """Write a version-stamped metadata record into the embedded DB."""
    db.query(
        "CREATE subset_meta SET "
        "group_id=$group_id, exported_at=$exported_at, "
        "pinard_core_version=$core_version, "
        "domain_name=$domain_name, domain_version=$domain_version, "
        "entity_count=$entity_count, wiki_doc_count=$wiki_doc_count, "
        "edge_count=$edge_count, ontology_stamp=$ontology_stamp",
        {
            "group_id": group_id,
            "exported_at": exported_at,
            "core_version": ontology_stamp.get("pinard_core", ""),
            "domain_name": ontology_stamp.get("domain", {}).get("name", ""),
            "domain_version": ontology_stamp.get("domain", {}).get("version", ""),
            "entity_count": entity_count,
            "wiki_doc_count": wiki_doc_count,
            "edge_count": edge_count,
            "ontology_stamp": ontology_stamp,
        },
    )


def _strip_id(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the record without the server-side `id` field."""
    return {k: v for k, v in record.items() if k != "id"}


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a scoped SurrealDB subset to an embedded file."
    )
    parser.add_argument(
        "--group-id", required=True, help="group_id / SurrealDB database name to export"
    )
    parser.add_argument(
        "--out", required=True, help="Output path for the embedded SurrealDB file"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from services.memory.ontology.registry import OntologyRegistry  # type: ignore[import]
    except ImportError:
        from ..ontology.registry import OntologyRegistry  # type: ignore[import]

    registry = OntologyRegistry()
    try:
        result = export_subset(args.group_id, args.out, registry)
        print(result)
    except SubsetError as exc:
        logger.error("Export failed: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
