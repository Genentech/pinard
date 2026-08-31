"""Dynamic SurrealDB schema generation from a ComposedOntology.

Generates DDL strings from the ontology rather than applying a static .surql
file — so every group_id database reflects its composed (core + domain) ontology.

Public API::

    ddl = generate_schema_ddl(composed)          # full schema as a string
    populate_ontology_meta(surreal, composed)     # upsert meta-model rows
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import SurrealClient
    from ..ontology.registry import ComposedOntology


def _to_snake(name: str) -> str:
    """Convert CamelCase to snake_case (e.g. DependsOn -> depends_on)."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


# ── Base DDL (static, applied to every group_id) ─────────────────────────────

_BASE_DDL = """\
-- ─── Namespace ────────────────────────────────────────────────────────────────
DEFINE NAMESPACE IF NOT EXISTS pinard;

-- ─── Entities ─────────────────────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name        ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS role        ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS description ON entity TYPE string   DEFAULT "";
DEFINE FIELD IF NOT EXISTS version     ON entity TYPE string   DEFAULT "1.0.0";
DEFINE FIELD IF NOT EXISTS provenance  ON entity TYPE string   DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at  ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at  ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS data        ON entity TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS manual_edit ON entity TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS embedding   ON entity TYPE option<array<float>>;

DEFINE INDEX IF NOT EXISTS entity_role_name ON entity FIELDS role, name UNIQUE;
DEFINE INDEX IF NOT EXISTS entity_embedding_hnsw
  ON entity FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;

DEFINE ANALYZER IF NOT EXISTS pinard_text TOKENIZERS blank, class FILTERS lowercase, snowball(english);
DEFINE INDEX IF NOT EXISTS entity_name_fts
  ON entity FIELDS name FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS entity_description_fts
  ON entity FIELDS description FULLTEXT ANALYZER pinard_text BM25;

-- ─── Wiki documents ───────────────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS wiki_doc SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS title        ON wiki_doc TYPE string;
DEFINE FIELD IF NOT EXISTS type         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS summary      ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS body         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS frontmatter  ON wiki_doc TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS path         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS content_hash ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS confidence   ON wiki_doc TYPE float   DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS status       ON wiki_doc TYPE string  DEFAULT "needs_review";
DEFINE FIELD IF NOT EXISTS created_at   ON wiki_doc TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at   ON wiki_doc TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS embedding    ON wiki_doc TYPE option<array<float>>;

DEFINE INDEX IF NOT EXISTS wiki_doc_path  ON wiki_doc FIELDS path UNIQUE;
DEFINE INDEX IF NOT EXISTS wiki_doc_title ON wiki_doc FIELDS title;
DEFINE INDEX IF NOT EXISTS wiki_doc_embedding_hnsw
  ON wiki_doc FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;
DEFINE INDEX IF NOT EXISTS wiki_doc_title_fts
  ON wiki_doc FIELDS title FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS wiki_doc_body_fts
  ON wiki_doc FIELDS body FULLTEXT ANALYZER pinard_text BM25;

-- ─── Wiki chunks (per-heading embeddings for fine-grained semantic recall) ──────
DEFINE TABLE IF NOT EXISTS wiki_chunk SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS parent_path  ON wiki_chunk TYPE string;
DEFINE FIELD IF NOT EXISTS heading      ON wiki_chunk TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS chunk_index  ON wiki_chunk TYPE int     DEFAULT 0;
DEFINE FIELD IF NOT EXISTS text         ON wiki_chunk TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS embedding    ON wiki_chunk TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at   ON wiki_chunk TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS wiki_chunk_embedding_hnsw
  ON wiki_chunk FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;
DEFINE INDEX IF NOT EXISTS wiki_chunk_text_fts
  ON wiki_chunk FIELDS text FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS wiki_chunk_parent
  ON wiki_chunk FIELDS parent_path;

-- ─── MR knowledge supersession ───────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS supersedes SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON supersedes TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON supersedes TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON supersedes TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON supersedes TYPE datetime DEFAULT time::now();

-- ─── Wiki relations ───────────────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS wiki_references SCHEMAFULL TYPE RELATION IN wiki_doc OUT wiki_doc;
DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_references TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at ON wiki_references TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS wiki_mentions SCHEMAFULL TYPE RELATION IN wiki_doc OUT entity;
DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_mentions TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at ON wiki_mentions TYPE datetime DEFAULT time::now();

-- ─── Ontology meta-tables ─────────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS ontology_entity_type SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS role        ON ontology_entity_type TYPE string;
DEFINE FIELD IF NOT EXISTS name        ON ontology_entity_type TYPE string;
DEFINE FIELD IF NOT EXISTS domain      ON ontology_entity_type TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS fields_json ON ontology_entity_type TYPE string DEFAULT "{}";
DEFINE FIELD IF NOT EXISTS version     ON ontology_entity_type TYPE string DEFAULT "1.0.0";
DEFINE INDEX IF NOT EXISTS ontology_entity_type_role ON ontology_entity_type FIELDS role UNIQUE;

DEFINE TABLE IF NOT EXISTS ontology_edge_type SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name            ON ontology_edge_type TYPE string;
DEFINE FIELD IF NOT EXISTS table_name      ON ontology_edge_type TYPE string;
DEFINE FIELD IF NOT EXISTS valid_pairs_json ON ontology_edge_type TYPE string DEFAULT "[]";
DEFINE FIELD IF NOT EXISTS domain          ON ontology_edge_type TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS version         ON ontology_edge_type TYPE string DEFAULT "1.0.0";
DEFINE INDEX IF NOT EXISTS ontology_edge_type_name ON ontology_edge_type FIELDS name UNIQUE;

DEFINE TABLE IF NOT EXISTS ontology_version SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS core_version    ON ontology_version TYPE string;
DEFINE FIELD IF NOT EXISTS domain_name     ON ontology_version TYPE option<string>;
DEFINE FIELD IF NOT EXISTS domain_version  ON ontology_version TYPE option<string>;
DEFINE FIELD IF NOT EXISTS applied_at      ON ontology_version TYPE datetime DEFAULT time::now();

-- ─── Staging tables (open-world classification) ───────────────────────────────
DEFINE TABLE IF NOT EXISTS entity_staging SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name             ON entity_staging TYPE string;
DEFINE FIELD IF NOT EXISTS proposed_role    ON entity_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS description      ON entity_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS rationale        ON entity_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS provenance       ON entity_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS occurrence_count ON entity_staging TYPE int    DEFAULT 1;
DEFINE FIELD IF NOT EXISTS data             ON entity_staging TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS embedding        ON entity_staging TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at       ON entity_staging TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at       ON entity_staging TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS entity_staging_name ON entity_staging FIELDS name UNIQUE;
DEFINE INDEX IF NOT EXISTS entity_staging_embedding_hnsw
  ON entity_staging FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;
DEFINE INDEX IF NOT EXISTS entity_staging_name_fts
  ON entity_staging FIELDS name FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS entity_staging_description_fts
  ON entity_staging FIELDS description FULLTEXT ANALYZER pinard_text BM25;

DEFINE TABLE IF NOT EXISTS edge_staging SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS from_name         ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS from_role         ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS to_name           ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS to_role           ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS proposed_relation ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS description       ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS rationale         ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS provenance        ON edge_staging TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS occurrence_count  ON edge_staging TYPE int    DEFAULT 1;
DEFINE FIELD IF NOT EXISTS data              ON edge_staging TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS embedding         ON edge_staging TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at        ON edge_staging TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at        ON edge_staging TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS edge_staging_dedup
  ON edge_staging FIELDS from_name, to_name, proposed_relation UNIQUE;
DEFINE INDEX IF NOT EXISTS edge_staging_embedding_hnsw
  ON edge_staging FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;
DEFINE INDEX IF NOT EXISTS edge_staging_from_name_fts
  ON edge_staging FIELDS from_name FULLTEXT ANALYZER pinard_text BM25;

-- ─── Ingest cursor (durable seq-cursor for postgres ingestion) ────────────────
DEFINE TABLE IF NOT EXISTS ingest_cursor SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS source     ON ingest_cursor TYPE string;
DEFINE FIELD IF NOT EXISTS seq        ON ingest_cursor TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS updated_at ON ingest_cursor TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS ingest_cursor_source ON ingest_cursor FIELDS source UNIQUE;

-- ─── Wiki curator cursor (durable timestamp-cursor for outbound synthesis) ───
DEFINE TABLE IF NOT EXISTS wiki_curator_cursor SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS source         ON wiki_curator_cursor TYPE string;
DEFINE FIELD IF NOT EXISTS last_synced_at ON wiki_curator_cursor TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS updated_at     ON wiki_curator_cursor TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS wiki_curator_cursor_source ON wiki_curator_cursor FIELDS source UNIQUE;
"""

_EDGE_TABLE_TEMPLATE = """\
DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON {table} TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON {table} TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON {table} TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at  ON {table} TYPE datetime DEFAULT time::now();
"""


def generate_schema_ddl(composed: "ComposedOntology") -> str:
    """Return a complete SurrealQL DDL string for the given composed ontology.

    Includes:
    - Static base tables (entity, wiki_doc, wiki_references, wiki_mentions)
    - Ontology meta-tables (ontology_entity_type, ontology_edge_type, ontology_version)
    - Staging tables (entity_staging, edge_staging)
    - One RELATION table per edge type in *composed* (core + domain)
    """
    parts = [_BASE_DDL, "-- ─── Graph edge tables (composed ontology) ──────────────────────────────────"]
    for edge_cls in composed.edge_types:
        table = _to_snake(edge_cls.__name__)
        parts.append(_EDGE_TABLE_TEMPLATE.format(table=table))
    return "\n".join(parts)


# ── Meta-model population ─────────────────────────────────────────────────────

def populate_ontology_meta(surreal: "SurrealClient", composed: "ComposedOntology") -> None:
    """Upsert ontology meta-model rows and version stamp from *composed*.

    Idempotent — safe to call on every schema apply.
    """
    domain_name = composed.version.domain_name or ""

    # ontology_entity_type — one row per active entity class
    for cls in composed.entity_types:
        role_field = cls.model_fields.get("role")
        role = role_field.default if role_field is not None else cls.__name__
        fields = {
            k: str(f.annotation)
            for k, f in cls.model_fields.items()
            if k not in ("role", "name", "description", "version")
        }
        surreal.query(
            "UPSERT type::record('ontology_entity_type', $role) SET "
            "role = $role, name = $name, domain = $domain, "
            "fields_json = $fields_json, version = $version",
            {
                "role": role,
                "name": cls.__name__,
                "domain": domain_name,
                "fields_json": json.dumps(fields),
                "version": composed.version.core_version,
            },
        )

    # ontology_edge_type — one row per active edge class
    for cls in composed.edge_types:
        table = _to_snake(cls.__name__)
        pairs = composed.edge_type_map.get(cls.__name__, [])
        surreal.query(
            "UPSERT type::record('ontology_edge_type', $name) SET "
            "name = $name, table_name = $table_name, valid_pairs_json = $pairs_json, "
            "domain = $domain, version = $version",
            {
                "name": cls.__name__,
                "table_name": table,
                "pairs_json": json.dumps(pairs),
                "domain": domain_name,
                "version": composed.version.core_version,
            },
        )

    # ontology_version — single stamp record (id = "current")
    surreal.query(
        "UPSERT type::record('ontology_version', 'current') SET "
        "core_version = $core_version, domain_name = $domain_name, "
        "domain_version = $domain_version, applied_at = time::now()",
        {
            "core_version": composed.version.core_version,
            "domain_name": composed.version.domain_name,
            "domain_version": composed.version.domain_version,
        },
    )


def read_ontology_version(surreal: "SurrealClient") -> "dict[str, Any] | None":
    """Return the stored ontology_version stamp, or None if not yet written."""
    results = surreal.query(
        "SELECT * FROM type::record('ontology_version', 'current')"
    )
    if results and results[0]:
        row = results[0]
        return row[0] if isinstance(row, list) else row
    return None
