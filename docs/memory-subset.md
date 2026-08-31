# Portable Memory Subset — SurrealDB Embedded Export & Local Load

Implements Amendment-01 Decision A7 / tasks.md §10.

## Overview

The central SurrealDB (server) stores all agent knowledge, scoped per `group_id`
(one SurrealDB database per vigne/pipeline). A **portable subset** is a snapshot
of one `group_id`'s data exported into a self-contained, embedded SurrealDB file
(`surrealkv://`) — no running server required.

This enables the **three-element ExoHub bundle**:

```
harness + babysitter process + <group_id>.surrealkv
```

All three are versioned together. An agent booted from the bundle loads its memory
locally at start, giving reproducible, auditable knowledge injection without a live
server dependency.

## Exporting a subset

```bash
python -m services.memory.surrealdb.subset \
    --group-id genomics-build \
    --out /path/to/genomics-build.surrealkv
```

Or from Python:

```python
from services.memory.surrealdb.subset import export_subset
from services.memory.ontology.registry import OntologyRegistry

registry = OntologyRegistry()
result = export_subset("genomics-build", "/path/to/genomics-build.surrealkv", registry)
# SubsetResult(group_id='genomics-build', entities=42, wiki_docs=3, edges=7, out='...')
```

The export:

1. Connects to the central SurrealDB (via `SURREAL_URL` / `SURREAL_PASS`).
2. SELECTs all `entity`, `wiki_doc`, and RELATE edge records for the given `group_id`.
3. Writes them to an embedded SurrealDB file (`surrealkv://`).
4. Stores a **version stamp** (`subset_meta` table) containing:
   - `group_id`
   - `exported_at` (ISO-8601 UTC)
   - `pinard_core_version` (from `pinard-core` package)
   - `domain_name` / `domain_version` (from the registered domain ontology, if any)
   - `entity_count`, `wiki_doc_count`, `edge_count`

## Loading at agent boot

Set `MEMORY_EMBEDDED_SUBSET` to the path of the exported file:

```bash
export MEMORY_EMBEDDED_SUBSET=/path/to/genomics-build.surrealkv
```

When `MEMORY_EMBEDDED_SUBSET` is set, the query handler opens the embedded file
instead of contacting the central SurrealDB server. If the file is missing or
fails to open, it falls back to the central server (fail-open).

### Querying from Python

```python
from services.memory.surrealdb.embedded_client import load_embedded_subset

with load_embedded_subset("/path/to/genomics-build.surrealkv", "genomics-build") as client:
    # Semantic recall (cosine scan — no HNSW index in embedded mode)
    hits = client.recall_cosine_scan(embedding=[...], limit=10)

    # Lexical lookup
    hits = client.lookup("OOM shard", limit=5)

    # Graph traversal
    neighbors = client.trace("diagnosis", "OOM on shard 47", "resolved_by")

    # Version stamp
    meta = client.query_meta()
    print(meta["pinard_core_version"], meta["exported_at"])
```

The embedded client exposes the same interface as `SurrealClient` for the three
typed intents (recall / lookup / trace), so `query_handler.py` uses either
transparently.

## ExoHub versioning

The subset file is a plain directory (SurrealKV on-disk format). Version it
alongside the process definition:

```
pinard/
  genomics-build/
    process.js         ← babysitter process definition (in repo)
    memory.surrealkv/  ← exported subset (versioned artifact)
```

Re-export the subset whenever significant new knowledge is ingested (e.g. after
a teaching session or a completed pipeline run). Commit the updated file to the
ExoHub release artifact.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MEMORY_EMBEDDED_SUBSET` | No | Path to an embedded SurrealDB subset file. When set, the query handler prefers this over the central server. |
| `SURREAL_URL` | For export | Central SurrealDB endpoint (default: `http://localhost:8000`). |
| `SURREAL_USER` | For export | Central SurrealDB root username (default: `root`). |
| `SURREAL_PASS` | For export | Central SurrealDB root password. |

## Recall method: cosine scan vs HNSW

The central server uses an **HNSW index** for O(log n) approximate nearest-neighbor
search. The embedded file uses a **full cosine scan** (`vector::similarity::cosine`).

This is intentional: HNSW index rebuild takes seconds per thousand records, which
is unacceptable at load time. For the expected subset sizes (≤ a few hundred
entities per pipeline scope), full cosine scan is adequate — the spike measured
< 5 ms at 58 records. If a subset grows beyond ~1000 entities, consider running
a periodic re-export that pre-builds the HNSW index offline.

## Ontology migration

Each subset carries the `pinard_core_version` and `domain_version` it was extracted
under. When these change:

- **Minor/patch bump** (same major): existing subsets remain readable. Re-export is
  recommended but not required. Check with `MigrationPolicy.check()`.
- **Major bump**: breaking change. Re-export the subset under the new ontology.
  The `MigrationPolicy.apply()` raises `MigrationError` for unsafe migrations;
  human review required.

See `packages/pinard-core/pinard_core/versioning.py` for the migration policy API.
