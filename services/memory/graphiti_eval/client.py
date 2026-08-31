"""Thin Graphiti SDK wrapper for the eval sidecar.

EVAL-ONLY — time-boxed (amendment-01 Decision A2). Not permanent architecture.

The LLM client used by Graphiti is driven by the same MEMORY_LLM_* env vars as
the rest of the memory service (no separate Anthropic-only config):

Environment variables:
    FALKORDB_URL            — FalkorDB bolt URL (default: bolt://localhost:6379)
    MEMORY_LLM_API          — Protocol adapter: ``openai-chat`` | ``anthropic-messages``
                              (default: ``anthropic-messages``)
    MEMORY_LLM_BASE_URL     — Endpoint override
    MEMORY_LLM_MODEL        — Model id (default per-adapter)
    MEMORY_LLM_AUTH         — Token source: ``google-sa`` | ``url`` | ``static-key``
    MEMORY_TOKEN_URL        — Pour-token URL (MEMORY_LLM_AUTH=url or auto)
    ANTHROPIC_API_KEY       — Direct key (MEMORY_LLM_AUTH=static-key, anthropic-messages)
    OPENAI_API_KEY          — Direct key (MEMORY_LLM_AUTH=static-key, openai-chat)
    GOOGLE_APPLICATION_CREDENTIALS — SA JSON path (MEMORY_LLM_AUTH=google-sa)
    MEMORY_EXTRACTION_MODEL — Legacy model override (use MEMORY_LLM_MODEL instead)
"""
from __future__ import annotations

import os
from typing import Any

from ..llm_client import build_llm_client

FALKORDB_URL = os.environ.get("FALKORDB_URL", "bolt://localhost:6379")


def build_graphiti(group_id: str) -> Any:
    """Construct and return a Graphiti instance scoped to *group_id*.

    The LLM client is selected by MEMORY_LLM_API (openai-chat → OpenAIClient,
    anthropic-messages → AnthropicClient). Token/auth from the shared
    MEMORY_LLM_* env vars.

    Returns the graphiti_core.Graphiti instance (caller must await
    graphiti.build_indices_and_constraints() before first use in async contexts).
    """
    from graphiti_core import Graphiti  # type: ignore[import]

    llm = build_llm_client()
    llm_client = llm.build_graphiti_llm_client()

    return Graphiti(
        uri=FALKORDB_URL,
        user="",
        password="",
        llm_client=llm_client,
        group_id=group_id,
    )


async def add_episode(
    graphiti: Any,
    group_id: str,
    name: str,
    content: str,
    source: str = "pinard",
    entity_types: list[Any] | None = None,
    edge_types: list[Any] | None = None,
) -> Any:
    """Add an episode to the Graphiti graph.

    entity_types and edge_types are passed from the composed ontology for
    prescribed-type extraction. When None, Graphiti uses its default extraction.
    """
    from graphiti_core.nodes import EpisodeType  # type: ignore[import]

    kwargs: dict[str, Any] = {
        "name": name,
        "episode_body": content,
        "source": EpisodeType.text,
        "source_description": source,
        "group_id": group_id,
    }
    if entity_types is not None:
        kwargs["entity_types"] = entity_types
    if edge_types is not None:
        kwargs["edge_types"] = edge_types

    return await graphiti.add_episode(**kwargs)


async def search(graphiti: Any, query: str, group_ids: list[str], limit: int = 10) -> list[Any]:
    """Search the Graphiti graph for facts matching *query*."""
    return await graphiti.search(query=query, group_ids=group_ids, num_results=limit)
