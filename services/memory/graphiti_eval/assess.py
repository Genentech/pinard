"""Graphiti eval assessment runner.

After ingesting SurrealDB data (via export.py + ingest.py), this script runs
a representative set of Graphiti search queries and prints:
  - NER/entity extraction results
  - Edge/relation extraction quality
  - Temporal deduplication observations (does Graphiti collapse duplicates?)

EVAL-ONLY — time-boxed (amendment-01 Decision A2). Output feeds the Phase-2
KG decision findings note (docs/memory-kg-evaluation.md).

Usage::

    python -m services.memory.graphiti_eval.assess --group-id my-vigne

Output: JSON Lines to stdout, one object per probe.  Each object has:
    {"probe": str, "query": str, "result_count": int,
     "facts": [str, ...], "entities": [str, ...]}

Additionally a summary is printed to stderr with counts and observations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from .client import build_graphiti
from .client import search as graphiti_search

log = logging.getLogger(__name__)

# Representative queries targeting different entity/edge types.
# Each probe has a name and a natural-language query string.
PROBES: list[dict[str, str]] = [
    {
        "probe": "task_dependency",
        "query": "What tasks depend on other tasks or gates?",
    },
    {
        "probe": "diagnosis_resolution",
        "query": "How are diagnoses resolved? What actions fix problems?",
    },
    {
        "probe": "log_pattern_signals",
        "query": "Which log patterns indicate problems or failures?",
    },
    {
        "probe": "artifact_production",
        "query": "What artifacts are produced by tasks or steps?",
    },
    {
        "probe": "environment_conditions",
        "query": "What environment conditions are required or breached?",
    },
    {
        "probe": "decision_triggers",
        "query": "What triggers decisions in the agent workflow?",
    },
    {
        "probe": "store_of_record",
        "query": "SurrealDB store of record memory layer",
    },
]


def _extract_facts(results: list[Any]) -> list[str]:
    """Extract human-readable fact strings from Graphiti search results."""
    facts = []
    for r in results:
        if isinstance(r, dict):
            fact = r.get("fact") or r.get("content") or r.get("name") or str(r)
        else:
            fact = str(r)
        facts.append(fact)
    return facts


def _extract_entity_names(results: list[Any]) -> list[str]:
    """Extract entity names from Graphiti search results."""
    names = []
    for r in results:
        if not isinstance(r, dict):
            continue
        # Graphiti returns edges with source/target node data.
        for key in ("source_node", "target_node"):
            node = r.get(key)
            if isinstance(node, dict):
                name = node.get("name") or node.get("uuid", "")
                if name:
                    names.append(name)
    return names


async def run_assessment(
    group_id: str,
    probes: list[dict[str, str]] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Run assessment probes against the Graphiti eval sidecar.

    Returns a list of probe result objects.
    """
    active_probes = probes if probes is not None else PROBES

    graphiti = build_graphiti(group_id)
    try:
        await graphiti.build_indices_and_constraints()
    except Exception as exc:
        log.error("Failed to connect to Graphiti/FalkorDB: %s", exc)
        raise

    results_out: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    duplicate_count = 0

    for probe in active_probes:
        probe_name = probe["probe"]
        query = probe["query"]
        log.info("Running probe %r: %r", probe_name, query)

        try:
            results = await graphiti_search(graphiti, query, [group_id], limit=limit)
        except Exception as exc:
            log.warning("Probe %r failed: %s", probe_name, exc)
            results_out.append(
                {"probe": probe_name, "query": query, "error": str(exc),
                 "result_count": 0, "facts": [], "entities": []}
            )
            continue

        facts = _extract_facts(results)
        entities = _extract_entity_names(results)

        # Temporal dedup observation: track facts we've seen across probes.
        new_facts = []
        for f in facts:
            if f in seen_facts:
                duplicate_count += 1
            else:
                seen_facts.add(f)
                new_facts.append(f)

        result_obj: dict[str, Any] = {
            "probe": probe_name,
            "query": query,
            "result_count": len(results),
            "facts": facts,
            "entities": entities,
            "new_facts": len(new_facts),
            "duplicate_facts": len(facts) - len(new_facts),
        }
        results_out.append(result_obj)

    # Append a summary object last.
    total_results = sum(r.get("result_count", 0) for r in results_out)
    results_out.append({
        "probe": "__summary__",
        "total_probes": len(active_probes),
        "total_results": total_results,
        "unique_facts": len(seen_facts),
        "cross_probe_duplicates": duplicate_count,
        "dedup_rate": (
            round(duplicate_count / max(total_results, 1), 3)
        ),
    })

    return results_out


def _main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stderr)

    parser = argparse.ArgumentParser(
        description="Run Graphiti eval assessment probes and print results as JSON Lines."
    )
    parser.add_argument("--group-id", required=True)
    parser.add_argument(
        "--limit", type=int, default=5, help="Max results per probe (default: 5)"
    )
    args = parser.parse_args()

    async def run() -> None:
        results = await run_assessment(args.group_id, limit=args.limit)
        for obj in results:
            print(json.dumps(obj, ensure_ascii=False))

        # Print summary to stderr for easy reading.
        summary = next((r for r in results if r.get("probe") == "__summary__"), {})
        log.info(
            "Assessment done — probes=%d total_results=%d unique_facts=%d dedup_rate=%.1f%%",
            summary.get("total_probes", 0),
            summary.get("total_results", 0),
            summary.get("unique_facts", 0),
            summary.get("dedup_rate", 0.0) * 100,
        )

    asyncio.run(run())


if __name__ == "__main__":
    _main()
