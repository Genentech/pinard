-- pinard memory layer — SurrealDB schema (static base only)
-- Apply against any group_id database after defining the namespace/database.
-- Run: surreal sql --endpoint <url> --user root --pass <pass> --file schema.surql
--
-- Namespace: pinard
-- Database:  one per group_id (vigne/pipeline scope), e.g. USE DB `genomics-build`;
-- The schema below uses the `pinard` namespace and a placeholder database `_template`.
-- The ingester applies it to each group_id database dynamically.
--
-- NOTE: Graph edge RELATION tables are no longer hardcoded here.
-- They are generated dynamically from the composed ontology (core + domain) by
-- services/memory/surrealdb/schema_gen.py::generate_schema_ddl().
-- This file is the fallback when no registry/group_id is available.

-- ─── Namespace ────────────────────────────────────────────────────────────────
DEFINE NAMESPACE IF NOT EXISTS pinard;
-- Note: no `USE NS` here — the client sets NS/DB context via SDK .use() before
-- executing this file, so a bare `USE NS` would wipe the database context.

-- ─── Template database (applied per group_id by the ingester) ────────────────
-- The ingester calls .use(namespace, group_id) then applies these definitions.

-- ─── Entities ─────────────────────────────────────────────────────────────────
-- Core entity types mirror the pinard-core Pydantic ontology.

DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name        ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS role        ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS description ON entity TYPE string   DEFAULT "";
DEFINE FIELD IF NOT EXISTS version     ON entity TYPE string   DEFAULT "1.0.0";
DEFINE FIELD IF NOT EXISTS provenance  ON entity TYPE string   DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at  ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at  ON entity TYPE datetime DEFAULT time::now();
-- Role-specific fields (stored as flexible record; absent when not applicable).
DEFINE FIELD IF NOT EXISTS data        ON entity TYPE object FLEXIBLE DEFAULT {};
-- Manual-edit guard: prevents re-extraction from clobbering human edits.
DEFINE FIELD IF NOT EXISTS manual_edit ON entity TYPE bool DEFAULT false;
-- 1024-dim embedding (Rosetta qwen3-emb-0.6b).
DEFINE FIELD IF NOT EXISTS embedding   ON entity TYPE option<array<float>>;

-- Unique index on (role, name) — prevents duplicate entities within a database.
DEFINE INDEX IF NOT EXISTS entity_role_name ON entity FIELDS role, name UNIQUE;

-- HNSW vector index for semantic recall (dim=1024, cosine distance).
DEFINE INDEX IF NOT EXISTS entity_embedding_hnsw
  ON entity
  FIELDS embedding
  HNSW DIMENSION 1024 DIST COSINE;

-- Full-text search index for lexical lookup.
DEFINE ANALYZER IF NOT EXISTS pinard_text TOKENIZERS blank, class FILTERS lowercase, snowball(english);
DEFINE INDEX IF NOT EXISTS entity_name_fts
  ON entity
  FIELDS name
  FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS entity_description_fts
  ON entity
  FIELDS description
  FULLTEXT ANALYZER pinard_text BM25;

-- ─── Wiki documents ───────────────────────────────────────────────────────────
-- Obsidian markdown pages: body + frontmatter + backlinks + embedding.

DEFINE TABLE IF NOT EXISTS wiki_doc SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS title        ON wiki_doc TYPE string;
DEFINE FIELD IF NOT EXISTS type         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS body         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS frontmatter  ON wiki_doc TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS path         ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS content_hash ON wiki_doc TYPE string  DEFAULT "";
DEFINE FIELD IF NOT EXISTS confidence   ON wiki_doc TYPE float   DEFAULT 1.0;
-- "needs_review" | "auto_serve" (Decision A8: ≥0.7 → auto_serve)
DEFINE FIELD IF NOT EXISTS status       ON wiki_doc TYPE string  DEFAULT "needs_review";
DEFINE FIELD IF NOT EXISTS created_at   ON wiki_doc TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at   ON wiki_doc TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS embedding    ON wiki_doc TYPE option<array<float>>;

-- path is the OKF concept ID and is the unique key; title is display-only
DEFINE INDEX IF NOT EXISTS wiki_doc_path  ON wiki_doc FIELDS path UNIQUE;
DEFINE INDEX IF NOT EXISTS wiki_doc_title ON wiki_doc FIELDS title;

DEFINE INDEX IF NOT EXISTS wiki_doc_embedding_hnsw
  ON wiki_doc
  FIELDS embedding
  HNSW DIMENSION 1024 DIST COSINE;

DEFINE INDEX IF NOT EXISTS wiki_doc_title_fts
  ON wiki_doc
  FIELDS title
  FULLTEXT ANALYZER pinard_text BM25;
DEFINE INDEX IF NOT EXISTS wiki_doc_body_fts
  ON wiki_doc
  FIELDS body
  FULLTEXT ANALYZER pinard_text BM25;

-- ─── Core graph edge tables (fallback only) ──────────────────────────────────
-- When schema_gen.py is used (registry + group_id provided to ensure_schema),
-- edge tables are generated from the composed ontology instead.
-- This section covers the static fallback path (schema_path only).

DEFINE TABLE IF NOT EXISTS depends_on SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON depends_on TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON depends_on TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON depends_on TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON depends_on TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS produces SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON produces TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON produces TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON produces TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON produces TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS consumes SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON consumes TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON consumes TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON consumes TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON consumes TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS indicates_problem SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON indicates_problem TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON indicates_problem TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON indicates_problem TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON indicates_problem TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS resolved_by SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON resolved_by TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON resolved_by TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON resolved_by TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON resolved_by TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS requires_condition SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON requires_condition TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON requires_condition TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON requires_condition TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON requires_condition TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS triggers_decision SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON triggers_decision TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON triggers_decision TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON triggers_decision TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON triggers_decision TYPE datetime DEFAULT time::now();

-- ─── MR knowledge supersession ─────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS supersedes SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS confidence  ON supersedes TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS description ON supersedes TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS data        ON supersedes TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at  ON supersedes TYPE datetime DEFAULT time::now();

-- ─── Wiki backlinks ───────────────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS wiki_references SCHEMAFULL TYPE RELATION IN wiki_doc OUT wiki_doc;
DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_references TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at ON wiki_references TYPE datetime DEFAULT time::now();

-- ─── Wiki ↔ entity cross-references ──────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS wiki_mentions SCHEMAFULL TYPE RELATION IN wiki_doc OUT entity;
DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_mentions TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at ON wiki_mentions TYPE datetime DEFAULT time::now();
