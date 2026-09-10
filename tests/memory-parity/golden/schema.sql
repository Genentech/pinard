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

-- entity_role_name UNIQUE removed: record id uniqueness suffices and the
-- UNIQUE index caused SurrealDB-3.x UPSERT self-conflicts (entity updates dropped).
DEFINE INDEX IF NOT EXISTS entity_role_name ON entity FIELDS role, name;
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
-- ingest_cursor_source index removed: record id uniqueness suffices and the
-- UNIQUE index caused SurrealDB-3.x UPSERT self-conflicts (cursor stayed 0).

-- ─── Wiki curator cursor (durable timestamp-cursor for outbound synthesis) ───
DEFINE TABLE IF NOT EXISTS wiki_curator_cursor SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS source         ON wiki_curator_cursor TYPE string;
DEFINE FIELD IF NOT EXISTS last_synced_at ON wiki_curator_cursor TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS updated_at     ON wiki_curator_cursor TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS wiki_curator_cursor_source ON wiki_curator_cursor FIELDS source UNIQUE;

-- ─── Graph edge tables (composed ontology) ──────────────────────────────────
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

