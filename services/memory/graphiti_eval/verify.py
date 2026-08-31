"""Graphiti eval sidecar — connectivity and round-trip verification.

EVAL-ONLY (amendment-01 Decision A2). Run this at container startup to confirm
that Graphiti can connect to FalkorDB, create a test graph, and query it back.

Usage (standalone):
    python -m services.memory.graphiti_eval.verify

Exit code 0 = success, 1 = failure (logs details to stderr).
"""
from __future__ import annotations

import asyncio
import sys
import logging

from .client import build_graphiti, add_episode, search

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TEST_GROUP = "pinard-graphiti-eval-verify"
TEST_EPISODE_NAME = "graphiti-verify-test"
TEST_CONTENT = (
    "The pinard memory layer uses SurrealDB as the store of record. "
    "Graphiti is used as a time-boxed evaluation sidecar for temporal KG analysis."
)


async def run_verify() -> bool:
    """Create a test episode and query it back. Returns True on success."""
    log.info("Building Graphiti client...")
    try:
        graphiti = build_graphiti(TEST_GROUP)
        await graphiti.build_indices_and_constraints()
    except Exception as exc:
        log.error("Failed to initialize Graphiti: %s", exc)
        return False

    log.info("Adding test episode...")
    try:
        await add_episode(
            graphiti,
            group_id=TEST_GROUP,
            name=TEST_EPISODE_NAME,
            content=TEST_CONTENT,
            source="pinard-verify",
        )
    except Exception as exc:
        log.error("Failed to add test episode: %s", exc)
        return False

    log.info("Querying test episode back...")
    try:
        results = await search(graphiti, "SurrealDB store of record", [TEST_GROUP], limit=5)
        if not results:
            log.error("Query returned no results — Graphiti round-trip failed")
            return False
        log.info("Round-trip OK — %d result(s) returned", len(results))
    except Exception as exc:
        log.error("Query failed: %s", exc)
        return False

    return True


def main() -> None:
    ok = asyncio.run(run_verify())
    if ok:
        log.info("Graphiti eval sidecar: verification PASSED")
        sys.exit(0)
    else:
        log.error("Graphiti eval sidecar: verification FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
