"""JSON Lines → Graphiti ingester for the eval sidecar.

Reads nodes.jsonl and edges.jsonl produced by export.py and feeds them into
the Graphiti+FalkorDB sidecar as episodes, one episode per entity node (with
its outgoing edge descriptions concatenated as context).

EVAL-ONLY — time-boxed (amendment-01 Decision A2).

Episode content strategy:
  Each node becomes one episode.  Any edges where `from_id` matches the node
  are appended to the episode body so that Graphiti's NER pipeline sees the
  full relational context in one shot.  This gives the LLM the best chance of
  extracting typed entity→edge→entity chains.

Usage::

    import asyncio
    from services.memory.graphiti_eval.ingest import ingest_jsonl
    from services.memory.graphiti_eval.client import build_graphiti

    async def main():
        graphiti = build_graphiti("my-vigne")
        await graphiti.build_indices_and_constraints()
        stats = await ingest_jsonl(graphiti, "my-vigne",
                                   nodes_path="nodes.jsonl",
                                   edges_path="edges.jsonl")
        print(stats)  # {"episodes": 42, "skipped": 0}

    asyncio.run(main())

CLI::

    python -m services.memory.graphiti_eval.ingest \\
        --group-id my-vigne \\
        --nodes nodes.jsonl \\
        --edges edges.jsonl
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .client import add_episode

log = logging.getLogger(__name__)


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _build_episode_content(node: dict[str, Any], edges: list[dict[str, Any]]) -> str:
    """Build episode text for a single node + its outgoing edges."""
    parts: list[str] = []

    role = node.get("role", "entity")
    name = node.get("name", "")
    description = node.get("description", "")
    data = node.get("data") or {}

    parts.append(f"[{role}] {name}")
    if description:
        parts.append(description)
    if data:
        parts.append("Properties: " + "; ".join(f"{k}={v}" for k, v in data.items()))

    for edge in edges:
        rel = edge.get("relation", "related_to")
        to_id = edge.get("to_id", "")
        edge_desc = edge.get("description", "")
        conf = edge.get("confidence", 1.0)
        line = f"  -{rel}-> {to_id}"
        if edge_desc:
            line += f" ({edge_desc})"
        if conf < 1.0:
            line += f" [confidence={conf:.2f}]"
        parts.append(line)

    return "\n".join(parts)


async def ingest_jsonl(
    graphiti: Any,
    group_id: str,
    nodes_path: str | Path = "nodes.jsonl",
    edges_path: str | Path = "edges.jsonl",
    entity_types: list[Any] | None = None,
    edge_types: list[Any] | None = None,
) -> dict[str, int]:
    """Ingest nodes.jsonl + edges.jsonl into Graphiti.

    Each node becomes one episode; outgoing edges are appended to the episode
    body for richer NER context.

    Returns {"episodes": N, "skipped": M} where skipped counts nodes for which
    the add_episode call failed.
    """
    nodes = _load_jsonl(nodes_path)
    edges = _load_jsonl(edges_path)

    # Build from_id → [edge, …] index for O(1) lookup per node.
    edge_index: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        fid = edge.get("from_id", "")
        edge_index.setdefault(fid, []).append(edge)

    episodes = 0
    skipped = 0

    for node in nodes:
        node_id = node.get("id", "")
        name = node.get("name") or node_id or "unnamed"
        outgoing = edge_index.get(node_id, [])
        content = _build_episode_content(node, outgoing)

        try:
            await add_episode(
                graphiti,
                group_id=group_id,
                name=f"surreal-export:{node_id}",
                content=content,
                source="surrealdb-export",
                entity_types=entity_types,
                edge_types=edge_types,
            )
            episodes += 1
            log.debug("Ingested episode for node %r", node_id)
        except Exception as exc:
            log.warning("Failed to ingest node %r: %s", node_id, exc)
            skipped += 1

    log.info(
        "Ingest complete — group_id=%r episodes=%d skipped=%d",
        group_id,
        episodes,
        skipped,
    )
    return {"episodes": episodes, "skipped": skipped}


# ── CLI entry point ───────────────────────────────────────────────────────────


def _main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Ingest SurrealDB JSON Lines export into Graphiti eval sidecar."
    )
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--nodes", default="nodes.jsonl")
    parser.add_argument("--edges", default="edges.jsonl")
    args = parser.parse_args()

    from .client import build_graphiti

    async def run() -> None:
        graphiti = build_graphiti(args.group_id)
        await graphiti.build_indices_and_constraints()
        stats = await ingest_jsonl(
            graphiti,
            group_id=args.group_id,
            nodes_path=args.nodes,
            edges_path=args.edges,
        )
        print(json.dumps(stats))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
