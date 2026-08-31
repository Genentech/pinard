"""Rosetta embedding client for the pinard memory layer.

Wraps POST /api/v1/embeddings at the configured Rosetta endpoint (OpenAI-compatible).
The API is OpenAI-compatible; no authentication token is required (network-level access).

Environment variables:
    ROSETTA_URL   — Rosetta base URL (default: https://embeddings.example.com)
    ROSETTA_MODEL — embedding model (default: qwen3-emb-0.6b, 1024-dim)

Usage::

    from services.memory.embeddings import embed, embed_batch

    vec = embed("use fix: prefix for all commits")  # → list[float], len == 1024
    vecs = embed_batch(["text one", "text two"])     # → list[list[float]]
"""
from __future__ import annotations

import os
from typing import Sequence

import httpx

ROSETTA_URL = os.environ.get(
    "ROSETTA_URL", "https://embeddings.example.com"
).rstrip("/")
ROSETTA_MODEL = os.environ.get("ROSETTA_MODEL", "qwen3-emb-0.6b")
EMBEDDING_DIM = 1024
_DEFAULT_TIMEOUT = 30.0


class EmbeddingError(RuntimeError):
    pass


def embed(
    text: str,
    model: str = ROSETTA_MODEL,
    url: str = ROSETTA_URL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[float]:
    """Embed a single text string. Returns a 1024-dim float vector."""
    return embed_batch([text], model=model, url=url, timeout=timeout)[0]


def embed_batch(
    texts: Sequence[str],
    model: str = ROSETTA_MODEL,
    url: str = ROSETTA_URL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[list[float]]:
    """Embed a batch of strings. Returns a list of 1024-dim float vectors.

    Raises EmbeddingError on non-2xx responses or unexpected payload shape.
    """
    if not texts:
        return []

    payload = {"model": model, "input": list(texts)}
    try:
        resp = httpx.post(
            f"{url}/api/v1/embeddings",
            json=payload,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise EmbeddingError(f"Rosetta request failed: {exc}") from exc

    if resp.status_code != 200:
        raise EmbeddingError(
            f"Rosetta returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        body = resp.json()
        data = body["data"]
    except (KeyError, ValueError) as exc:
        raise EmbeddingError(f"Unexpected Rosetta response shape: {exc}") from exc

    # Sort by index in case the API returns out-of-order results.
    data.sort(key=lambda d: d.get("index", 0))
    vectors = [d["embedding"] for d in data]

    for i, vec in enumerate(vectors):
        if len(vec) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"Expected {EMBEDDING_DIM}-dim embedding at index {i}, got {len(vec)}"
            )
    return vectors
