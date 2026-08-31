"""SurrealDB → JSON Lines exporter for the Graphiti eval sidecar.

Reads all entity nodes and RELATE edges from a SurrealDB group_id database
and writes them as NDJSON to nodes.jsonl and edges.jsonl.

EVAL-ONLY — time-boxed (amendment-01 Decision A2). Feeds the Graphiti+FalkorDB
sidecar for temporal-KG evaluation.

Node record shape (one JSON object per line):
    {"id": str, "role": str, "name": str, "description": str, "data": dict}

Edge record shape (one JSON object per line):
    {"from_id": str, "to_id": str, "relation": str,
     "confidence": float, "description": str, "data": dict}

Usage::

    from services.memory.graphiti_eval.export import export_jsonl
    from services.memory.surrealdb.client import SurrealClient

    with SurrealClient(group_id="my-vigne") as client:
        stats = export_jsonl(client, nodes_path="nodes.jsonl", edges_path="edges.jsonl")
    print(stats)  # {"nodes": 42, "edges": 17}

CLI::

    python -m services.memory.graphiti_eval.export \\
        --group-id my-vigne \\
        --nodes-out nodes.jsonl \\
        --edges-out edges.jsonl
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Relations registered in the pinard-core edge-type map (snake_case SurrealDB table names).
_CORE_RELATIONS = [
    "depends_on",
    "produces",
    "consumes",
    "indicates_problem",
    "resolved_by",
    "requires_condition",
    "triggers_decision",
]


def _extract_record_id(raw_id: Any) -> str:
    """Normalise a SurrealDB record ID to a plain string.

    SurrealDB returns IDs as either a plain string "table:ulid" or a dict
    {"tb": "table", "id": {"String": "ulid"}} depending on the API version.
    """
    if isinstance(raw_id, str):
        return raw_id
    if isinstance(raw_id, dict):
        tb = raw_id.get("tb", "")
        inner = raw_id.get("id", {})
        if isinstance(inner, dict):
            inner_val = next(iter(inner.values()), "")
        else:
            inner_val = str(inner)
        return f"{tb}:{inner_val}" if tb else str(inner_val)
    return str(raw_id)


def export_nodes(client: Any, out_path: str | Path) -> int:
    """Query all entity records and write to *out_path* as NDJSON.

    Returns the number of records written.
    """
    results = client.query("SELECT id, role, name, description, data FROM entity")
    records: list[dict[str, Any]] = results[0].get("result", []) if results else []

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with out.open("w", encoding="utf-8") as fh:
        for rec in records:
            node = {
                "id": _extract_record_id(rec.get("id", "")),
                "role": rec.get("role", ""),
                "name": rec.get("name", ""),
                "description": rec.get("description", ""),
                "data": rec.get("data") or {},
            }
            fh.write(json.dumps(node, ensure_ascii=False) + "\n")
            written += 1

    log.info("Exported %d nodes → %s", written, out_path)
    return written


def export_edges(
    client: Any,
    out_path: str | Path,
    relations: list[str] | None = None,
) -> int:
    """Query all RELATE edges and write to *out_path* as NDJSON.

    Iterates over each relation table (snake_case) and UNIONs results.
    Returns the number of edge records written.
    """
    relation_names = relations if relations is not None else _CORE_RELATIONS

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with out.open("w", encoding="utf-8") as fh:
        for rel in relation_names:
            try:
                results = client.query(
                    f"SELECT in, out, confidence, description, data FROM {rel}"
                )
            except Exception as exc:
                # The relation table may not exist if no edges of this type were created.
                log.debug("Skipping relation %r (query failed: %s)", rel, exc)
                continue

            records = results[0].get("result", []) if results else []
            for rec in records:
                edge = {
                    "from_id": _extract_record_id(rec.get("in", "")),
                    "to_id": _extract_record_id(rec.get("out", "")),
                    "relation": rel,
                    "confidence": rec.get("confidence", 1.0),
                    "description": rec.get("description", ""),
                    "data": rec.get("data") or {},
                }
                fh.write(json.dumps(edge, ensure_ascii=False) + "\n")
                written += 1

    log.info("Exported %d edges → %s", written, out_path)
    return written


def export_jsonl(
    client: Any,
    nodes_path: str | Path = "nodes.jsonl",
    edges_path: str | Path = "edges.jsonl",
    relations: list[str] | None = None,
) -> dict[str, int]:
    """Export both nodes and edges. Returns {"nodes": N, "edges": M}."""
    n = export_nodes(client, nodes_path)
    e = export_edges(client, edges_path, relations=relations)
    return {"nodes": n, "edges": e}


# ── CLI entry point ───────────────────────────────────────────────────────────


def _main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Export SurrealDB entities and edges to JSON Lines for Graphiti eval."
    )
    parser.add_argument("--group-id", required=True, help="SurrealDB database name (group_id)")
    parser.add_argument("--nodes-out", default="nodes.jsonl", help="Output path for nodes")
    parser.add_argument("--edges-out", default="edges.jsonl", help="Output path for edges")
    parser.add_argument(
        "--surreal-url",
        default=os.environ.get("SURREAL_URL", "http://localhost:8000"),
    )
    parser.add_argument("--surreal-user", default=os.environ.get("SURREAL_USER", "root"))
    parser.add_argument("--surreal-pass", default=os.environ.get("SURREAL_PASS", ""))
    args = parser.parse_args()

    from services.memory.surrealdb.client import SurrealClient

    with SurrealClient(
        group_id=args.group_id,
        url=args.surreal_url,
        user=args.surreal_user,
        password=args.surreal_pass,
    ) as client:
        stats = export_jsonl(client, nodes_path=args.nodes_out, edges_path=args.edges_out)

    print(json.dumps(stats))


if __name__ == "__main__":
    _main()
