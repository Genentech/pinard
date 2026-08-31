"""SurrealDB client for the pinard memory layer.

Uses the official `surrealdb` Python SDK (CBOR transport) instead of a
hand-rolled HTTP JSON-RPC client.  The SDK:
- sends `Surreal-NS`/`Surreal-DB` headers (correct for SurrealDB ≥3.x),
- uses CBOR so string values are never re-parsed as SurrealQL,
- raises on per-statement ERR natively.

Environment variables:
    SURREAL_URL   — SurrealDB endpoint, e.g. http://localhost:8000 (default)
    SURREAL_USER  — root username (default: root)
    SURREAL_PASS  — root password (required)
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("pinard.memory.surrealdb")

# Path to the canonical schema file (applied idempotently on first DB touch).
SCHEMA_PATH = Path(__file__).parent / "schema.surql"

# Module-level cache: group_ids whose schema has already been applied this process.
_schema_applied: set[str] = set()

# Lazy module-level reference to the surrealdb SDK Surreal factory.
# Populated on first use (or at import time if the SDK is already loaded).
# Cannot be imported at the top of the file because our local package directory
# is also named `surrealdb/`, causing a circular import before this submodule
# finishes loading. The sentinel None is replaced by _get_surreal() below.
_Surreal: Any = None


def _get_surreal() -> Any:
    """Return the surrealdb SDK Surreal factory, loading it lazily if needed.

    Uses importlib.import_module with an explicit path to the installed SDK's
    top-level module, bypassing any local `surrealdb/` package in sys.path that
    would shadow the real SDK when this file's parent directory is on sys.path.
    """
    global _Surreal
    if _Surreal is None:
        # Find the real surrealdb SDK by looking for the installed distribution
        # rather than relying on normal import resolution (which would find our
        # local `surrealdb/` package first when running from services/memory/).
        #
        # Strategy: temporarily remove this package's directory from sys.path,
        # import the real SDK, then restore sys.path.
        _this_dir = str(Path(__file__).parent.parent)  # services/memory/
        _patched = _this_dir in sys.path
        if _patched:
            sys.path.remove(_this_dir)
        try:
            _sdk = importlib.import_module("surrealdb")
            _Surreal = _sdk.Surreal
        finally:
            if _patched:
                sys.path.insert(0, _this_dir)
    return _Surreal


# Re-export as a patchable module-level name so tests can do:
#   patch("surrealdb.client.Surreal", return_value=mock_db)
# The name is populated lazily; _get_surreal() keeps it in sync.
def Surreal(url: str) -> Any:  # noqa: N802
    """Thin shim that delegates to the real SDK Surreal factory.

    Exists solely so ``patch('surrealdb.client.Surreal', ...)`` works in tests.
    """
    return _get_surreal()(url)


class SurrealError(RuntimeError):
    pass


class SurrealClient:
    """Synchronous SurrealDB SDK client scoped to a group_id database."""

    NAMESPACE = "pinard"

    def __init__(
        self,
        group_id: str,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.group_id = group_id
        self._url = (url or os.environ.get("SURREAL_URL", "http://localhost:8000")).rstrip("/")
        self._user = user or os.environ.get("SURREAL_USER", "root")
        self._pass = password or os.environ.get("SURREAL_PASS", "")
        self._db = Surreal(self._url)
        # BlockingHttpSurrealConnection initialises its requests.Session on __enter__.
        self._db.__enter__()
        self._db.signin({"username": self._user, "password": self._pass})
        self._db.use(self.NAMESPACE, group_id)

    def close(self) -> None:
        try:
            self._db.__exit__(None, None, None)
        except Exception:
            pass

    def __enter__(self) -> "SurrealClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    # Base tables that must be SCHEMAFULL; used by _migrate_schemaless_tables.
    _SCHEMAFULL_TABLES = frozenset({
        "entity", "wiki_doc", "wiki_chunk",
        "entity_staging", "edge_staging",
        "ontology_entity_type", "ontology_edge_type",
        "ontology_version",
        "ingest_cursor",
        "wiki_references", "wiki_mentions",
    })

    def _migrate_schemaless_tables(self) -> None:
        """Detect any base table that exists as SCHEMALESS and migrate it to SCHEMAFULL.

        `DEFINE TABLE IF NOT EXISTS <t> SCHEMAFULL` is a no-op on an existing
        SCHEMALESS table, so `DEFINE FIELD … FLEXIBLE` then fails.  This method
        runs `DEFINE TABLE OVERWRITE <t> SCHEMAFULL` for every affected table —
        SurrealDB preserves existing rows — before the schema DDL is applied.
        Idempotent: no-op on fresh or already-SCHEMAFULL databases.
        """
        try:
            raw = self._db.query_raw("INFO FOR DB")
        except Exception as exc:
            logger.debug("_migrate_schemaless_tables: INFO FOR DB failed: %s", exc)
            return

        # The SDK wraps the result as {result: [{status: OK, result: <info-dict>}]}.
        info: dict[str, Any] = {}
        try:
            result_list = raw.get("result", [])
            if result_list and isinstance(result_list[0], dict):
                first = result_list[0]
                if first.get("status") == "OK":
                    info = first.get("result", {}) or {}
        except Exception:
            return

        tables: dict[str, Any] = info.get("tables", {}) if isinstance(info, dict) else {}
        for table_name, table_def in tables.items():
            if table_name not in self._SCHEMAFULL_TABLES:
                continue
            # table_def is either a string (the DDL) or a dict with a 'kind' key.
            definition = table_def if isinstance(table_def, str) else str(table_def)
            if "SCHEMALESS" in definition.upper() and "SCHEMAFULL" not in definition.upper():
                logger.warning(
                    "Migrating SCHEMALESS table %r to SCHEMAFULL in database %r",
                    table_name, self.group_id,
                )
                try:
                    overwrite_sql = f"DEFINE TABLE OVERWRITE {table_name} SCHEMAFULL"
                    overwrite_raw = self._db.query_raw(overwrite_sql)
                    stmts = overwrite_raw.get("result", [])
                    for stmt in stmts:
                        if isinstance(stmt, dict) and stmt.get("status") == "ERR":
                            logger.warning(
                                "Failed to migrate %r to SCHEMAFULL: %s",
                                table_name, stmt.get("result"),
                            )
                except Exception as exc:
                    logger.warning("Error migrating table %r: %s", table_name, exc)

    def apply_schema(self, schema_path: str) -> None:
        """Execute a .surql schema file against this client's database.

        Checks every per-statement result for ERR and raises on failure.
        """
        with open(schema_path) as f:
            sql = f.read()
        raw = self._db.query_raw(sql)
        self._db.check_response_for_error(raw, "apply_schema")
        self._db.check_response_for_result(raw, "apply_schema")
        for stmt in raw["result"]:
            if isinstance(stmt, dict) and stmt.get("status") == "ERR":
                raise SurrealError(f"Schema statement failed: {stmt.get('result')}")

    def ensure_schema(
        self,
        schema_path: str | Path = SCHEMA_PATH,
        registry: Any = None,
        group_id: str | None = None,
    ) -> None:
        """Apply the schema idempotently — once per (group_id, version) per process.

        If *registry* and *group_id* are provided the schema is generated
        dynamically from the composed ontology (core + domain edges + meta-tables
        + staging tables).  Otherwise the static *schema_path* file is applied.
        """
        _gid = group_id or self.group_id
        self._migrate_schemaless_tables()
        if registry is not None and _gid:
            from .schema_gen import generate_schema_ddl, populate_ontology_meta, read_ontology_version
            from ..ontology.versioning import MigrationPolicy, OntologyVersion

            composed = registry.compose(_gid)
            cache_key = (_gid, composed.version.core_version, composed.version.domain_name or "", composed.version.domain_version or "")
            if cache_key not in _schema_applied:
                # Check for version mismatch before applying.
                try:
                    stored = read_ontology_version(self)
                    if stored and stored.get("core_version"):
                        from_ver = OntologyVersion(
                            core_version=stored["core_version"],
                            domain_name=stored.get("domain_name"),
                            domain_version=stored.get("domain_version"),
                        )
                        policy = MigrationPolicy(from_version=from_ver, to_version=composed.version)
                        result = policy.check()
                        if result.needed:
                            logger.warning(
                                "Ontology version change detected for %s: %s",
                                _gid, result.notes,
                            )
                except Exception:
                    pass  # no stored version yet — first apply

                ddl = generate_schema_ddl(composed)
                with tempfile.NamedTemporaryFile(
                    suffix=".surql", mode="w", delete=False, encoding="utf-8"
                ) as f:
                    f.write(ddl)
                    tmp_path = f.name
                try:
                    self.apply_schema(tmp_path)
                finally:
                    import os as _os
                    _os.unlink(tmp_path)

                populate_ontology_meta(self, composed)
                _schema_applied.add(cache_key)
        else:
            if self.group_id in _schema_applied:
                return
            self.apply_schema(str(schema_path))
            _schema_applied.add(self.group_id)

    # ── Generic query ─────────────────────────────────────────────────────────

    def query(self, sql: str, vars: dict[str, Any] | None = None) -> list[Any]:
        """Run a SurrealQL query and return all result sets.

        For single-statement queries use self._db.query() directly (raises on ERR).
        This method uses query_raw so callers get all result sets for multi-statement
        SQL.  Each element is the `result` list of one statement.
        """
        raw = self._db.query_raw(sql, vars)
        self._db.check_response_for_error(raw, "query")
        self._db.check_response_for_result(raw, "query")
        results = []
        for stmt in raw["result"]:
            if isinstance(stmt, dict) and stmt.get("status") == "ERR":
                raise SurrealError(f"Query statement failed: {stmt.get('result')}")
            if isinstance(stmt, dict):
                results.append(stmt.get("result", []))
            else:
                results.append(stmt)
        return results

    # ── Entity operations ─────────────────────────────────────────────────────

    def upsert_entity(
        self,
        role: str,
        name: str,
        description: str = "",
        data: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        version: str = "1.0.0",
        provenance: str = "",
    ) -> dict[str, Any]:
        """Upsert an entity record by deterministic id. Returns the record.

        When the existing row has manual_edit = true the description and
        embedding are preserved — extraction must not clobber human edits.
        All other fields (role, name, data, provenance, version) are always
        updated.
        """
        rid = hashlib.sha256(f"{role}\x00{name}".encode()).hexdigest()[:32]
        # Use SurrealDB conditional value expressions to guard manual edits:
        # if the row already has manual_edit = true keep the stored description
        # and embedding, otherwise overwrite them from the extraction result.
        sql = (
            "UPSERT type::record('entity', $rid) SET "
            "role = $role, name = $name, version = $version, data = $data, "
            "provenance = $provenance, updated_at = time::now(), "
            "description = IF manual_edit = true THEN description ELSE $description END, "
            "embedding = IF manual_edit = true THEN embedding ELSE $embedding END"
        )
        vars: dict[str, Any] = {
            "rid": rid,
            "role": role,
            "name": name,
            "description": description,
            "version": version,
            "data": data or {},
            "provenance": provenance,
            "embedding": embedding,
        }
        results = self.query(sql, vars)
        if results and results[0]:
            row = results[0]
            return row[0] if isinstance(row, list) else row
        return {}

    def update_entity_description(
        self,
        entity_id: str,
        description: str,
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Update description and embedding of an existing entity, marking it as
        manually edited.  Preserves role, name, provenance, data, and version.

        *entity_id* must be a SurrealDB record id string in
        ``table:record_id`` format (e.g. ``entity:abc123``).
        Returns the updated record, or an empty dict if not found.
        """
        sql = (
            "UPDATE type::record($id) SET "
            "description = $description, embedding = $embedding, "
            "manual_edit = true, updated_at = time::now()"
        )
        try:
            results = self.query(sql, {"id": entity_id, "description": description, "embedding": embedding})
        except SurrealError:
            return {}
        if not results:
            return {}
        row = results[0]
        if isinstance(row, list):
            return row[0] if row else {}
        return row if row else {}

    def upsert_entity_staging(
        self,
        name: str,
        proposed_role: str = "",
        description: str = "",
        rationale: str = "",
        provenance: str = "",
        data: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Upsert a record into entity_staging for open-world unclassified entities."""
        rid = hashlib.sha256(f"staging\x00{name}".encode()).hexdigest()[:32]
        sql = (
            "UPSERT type::record('entity_staging', $rid) SET "
            "name = $name, proposed_role = $proposed_role, description = $description, "
            "rationale = $rationale, provenance = $provenance, data = $data, "
            "occurrence_count = (SELECT VALUE occurrence_count FROM type::record('entity_staging', $rid))[0] ?? 0 + 1, "
            "updated_at = time::now()"
        )
        vars: dict[str, Any] = {
            "rid": rid,
            "name": name,
            "proposed_role": proposed_role,
            "description": description,
            "rationale": rationale,
            "provenance": provenance,
            "data": data or {},
        }
        if embedding is not None:
            sql += ", embedding = $embedding"
            vars["embedding"] = embedding
        results = self.query(sql, vars)
        if results and results[0]:
            row = results[0]
            return row[0] if isinstance(row, list) else row
        return {}

    def upsert_edge_staging(
        self,
        from_name: str,
        from_role: str,
        to_name: str,
        to_role: str,
        proposed_relation: str = "",
        description: str = "",
        rationale: str = "",
        provenance: str = "",
        data: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Upsert a record into edge_staging for open-world unclassified edges."""
        key = f"staging_edge\x00{from_name}\x00{proposed_relation}\x00{to_name}"
        rid = hashlib.sha256(key.encode()).hexdigest()[:32]
        sql = (
            "UPSERT type::record('edge_staging', $rid) SET "
            "from_name = $from_name, from_role = $from_role, "
            "to_name = $to_name, to_role = $to_role, "
            "proposed_relation = $proposed_relation, description = <string>$description, "
            "rationale = $rationale, provenance = $provenance, data = $data, "
            "occurrence_count = (SELECT VALUE occurrence_count FROM type::record('edge_staging', $rid))[0] ?? 0 + 1, "
            "updated_at = time::now()"
        )
        vars: dict[str, Any] = {
            "rid": rid,
            "from_name": from_name,
            "from_role": from_role,
            "to_name": to_name,
            "to_role": to_role,
            "proposed_relation": proposed_relation,
            "description": description,
            "rationale": rationale,
            "provenance": provenance,
            "data": data or {},
        }
        if embedding is not None:
            sql += ", embedding = $embedding"
            vars["embedding"] = embedding
        results = self.query(sql, vars)
        if results and results[0]:
            row = results[0]
            return row[0] if isinstance(row, list) else row
        return {}

    def relate(
        self,
        from_role: str,
        from_name: str,
        relation: str,
        to_role: str,
        to_name: str,
        confidence: float = 1.0,
        description: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a RELATE edge between two entities (looked up by role+name).

        relation must be a snake_case edge table name, e.g. 'depends_on'.
        """
        # Resolve endpoints via the same deterministic ids upsert_entity uses,
        # rather than an inline `(SELECT ...)[0].id` subquery (invalid in a
        # RELATE arrow on SurrealDB 3.x). Entities must already exist.
        from_rid = hashlib.sha256(f"{from_role}\x00{from_name}".encode()).hexdigest()[:32]
        to_rid = hashlib.sha256(f"{to_role}\x00{to_name}".encode()).hexdigest()[:32]
        # relation is a schema-defined edge-table identifier (cannot be bound); guard it.
        rel = "".join(ch for ch in relation if ch.isalnum() or ch == "_")
        if not rel:
            raise SurrealError(f"invalid relation name: {relation!r}")
        sql = (
            f"RELATE (type::record('entity', $from_rid))->{rel}->(type::record('entity', $to_rid)) "
            "SET confidence = $confidence, description = <string>$description, data = $data"
        )
        results = self.query(
            sql,
            {
                "from_rid": from_rid,
                "to_rid": to_rid,
                "confidence": confidence,
                "description": description,
                "data": data or {},
            },
        )
        if results and results[0]:
            row = results[0]
            return row[0] if isinstance(row, list) else row
        return {}

    # ── Staging queries (read-side for ontology gardener) ──────────────────────

    def list_entity_staging(
        self,
        min_occurrence: int = 1,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return entity_staging rows with occurrence_count >= *min_occurrence*.

        Results are ordered by occurrence_count descending (most recurrent first).
        """
        sql = (
            "SELECT * FROM entity_staging "
            "WHERE occurrence_count >= $min_occurrence "
            "ORDER BY occurrence_count DESC LIMIT $limit"
        )
        results = self.query(sql, {"min_occurrence": min_occurrence, "limit": limit})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    def list_edge_staging(
        self,
        min_occurrence: int = 1,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return edge_staging rows with occurrence_count >= *min_occurrence*.

        Results are ordered by occurrence_count descending (most recurrent first).
        """
        sql = (
            "SELECT * FROM edge_staging "
            "WHERE occurrence_count >= $min_occurrence "
            "ORDER BY occurrence_count DESC LIMIT $limit"
        )
        results = self.query(sql, {"min_occurrence": min_occurrence, "limit": limit})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    # ── Ingest cursor (durable seq-cursor for postgres ingestion) ─────────────

    def get_ingest_cursor(self, source: str) -> int:
        """Return the last ingested seq for *source*, or 0 if not yet persisted."""
        results = self.query(
            "SELECT VALUE seq FROM type::record('ingest_cursor', $source)",
            {"source": source},
        )
        if results and results[0]:
            row = results[0]
            val = row[0] if isinstance(row, list) else row
            if val is not None:
                return int(val)
        return 0

    def set_ingest_cursor(self, source: str, seq: int) -> None:
        """Persist *seq* as the last ingested position for *source*."""
        self.query(
            "UPSERT type::record('ingest_cursor', $source) SET "
            "source = $source, seq = $seq, updated_at = time::now()",
            {"source": source, "seq": seq},
        )

    # ── Recall (semantic vector search) ──────────────────────────────────────

    def recall(
        self,
        embedding: list[float],
        limit: int = 10,
        role_filter: str | None = None,
        ef: int = 100,
    ) -> list[dict[str, Any]]:
        """Find nearest entities using the HNSW KNN index (<|K, EF|> operator).

        SurrealDB 3.2.0 requires K and EF to be integer literals in the KNN
        operator — bound parameters are rejected with "expected an unsigned
        integer".  $vec remains a bound parameter (strings/floats are fine).
        """
        k = int(limit)
        ef_int = int(ef)
        if role_filter:
            sql = (
                f"SELECT *, vector::distance::knn() AS dist FROM entity "
                f"WHERE embedding <|{k},{ef_int}|> $vec AND role = $role "
                f"ORDER BY dist"
            )
            vars: dict[str, Any] = {"vec": embedding, "role": role_filter}
        else:
            sql = (
                f"SELECT *, vector::distance::knn() AS dist FROM entity "
                f"WHERE embedding <|{k},{ef_int}|> $vec "
                f"ORDER BY dist"
            )
            vars = {"vec": embedding}
        results = self.query(sql, vars)
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    def recall_cosine_scan(
        self,
        embedding: list[float],
        limit: int = 10,
        role_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fallback full-scan cosine similarity recall (no index required)."""
        where = "WHERE embedding IS NOT NULL"
        if role_filter:
            where += " AND role = $role"
        sql = (
            f"SELECT *, vector::similarity::cosine(embedding, $vec) AS score "
            f"FROM entity {where} "
            f"ORDER BY score DESC LIMIT $limit"
        )
        vars: dict[str, Any] = {"vec": embedding, "limit": limit}
        if role_filter:
            vars["role"] = role_filter
        results = self.query(sql, vars)
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    # ── Lookup (full-text search) ─────────────────────────────────────────────

    def lookup(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        """Find entities by full-text match on name/description."""
        sql = (
            "SELECT *, math::max([search::score(1), search::score(2)]) AS score FROM entity "
            "WHERE name @1@ $text OR description @2@ $text "
            "ORDER BY score DESC LIMIT $limit"
        )
        results = self.query(sql, {"text": text, "limit": limit})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    # ── Trace (graph traversal) ───────────────────────────────────────────────

    def trace(
        self,
        from_role: str,
        from_name: str,
        relation: str,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Traverse graph edges from an entity up to *depth* hops."""
        sql = (
            f"SELECT ->{relation}->entity.* AS neighbors "
            f"FROM entity WHERE role = $role AND name = $name LIMIT 1"
        )
        results = self.query(sql, {"role": from_role, "name": from_name})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    # ── Wiki recall (read-side) ──────────────────────────────────────────────

    def recall_wiki(
        self,
        embedding: list[float],
        limit: int = 10,
        include_needs_review: bool = False,
        ef: int = 100,
    ) -> list[dict[str, Any]]:
        """Find nearest wiki_doc pages using the HNSW KNN index.

        Returns only 'auto_serve' pages by default; pass include_needs_review=True
        to also include 'needs_review' pages.  K and EF must be integer literals.
        """
        k = int(limit)
        ef_int = int(ef)
        if include_needs_review:
            sql = (
                f"SELECT path, title, type, summary, body, confidence, status, "
                f"vector::distance::knn() AS dist FROM wiki_doc "
                f"WHERE embedding <|{k},{ef_int}|> $vec "
                f"ORDER BY dist"
            )
        else:
            sql = (
                f"SELECT path, title, type, summary, body, confidence, status, "
                f"vector::distance::knn() AS dist FROM wiki_doc "
                f"WHERE embedding <|{k},{ef_int}|> $vec AND status = 'auto_serve' "
                f"ORDER BY dist"
            )
        results = self.query(sql, {"vec": embedding})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    def lookup_wiki(
        self,
        text: str,
        limit: int = 10,
        include_needs_review: bool = False,
    ) -> list[dict[str, Any]]:
        """Find wiki_doc pages by full-text match on title/body."""
        if include_needs_review:
            sql = (
                "SELECT path, title, type, summary, body, confidence, status, "
                "math::max([search::score(1), search::score(2)]) AS score FROM wiki_doc "
                "WHERE title @1@ $text OR body @2@ $text "
                "ORDER BY score DESC LIMIT $limit"
            )
        else:
            sql = (
                "SELECT path, title, type, summary, body, confidence, status, "
                "math::max([search::score(1), search::score(2)]) AS score FROM wiki_doc "
                "WHERE (title @1@ $text OR body @2@ $text) AND status = 'auto_serve' "
                "ORDER BY score DESC LIMIT $limit"
            )
        results = self.query(sql, {"text": text, "limit": limit})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    # ── Exact fetch (by path / record id) ──────────────────────────────────

    def fetch_wiki_by_path(self, path: str) -> dict[str, Any] | None:
        """Return a single wiki_doc whose path field equals *path*, or None."""
        sql = (
            "SELECT path, title, body, confidence, status FROM wiki_doc "
            "WHERE path = $path LIMIT 1"
        )
        results = self.query(sql, {"path": path})
        if not results:
            return None
        row = results[0]
        if isinstance(row, list):
            return row[0] if row else None
        return row if row else None

    def fetch_entity_by_role_name(self, role: str, name: str) -> dict[str, Any] | None:
        """Return a single entity by deterministic (role, name) key, or None."""
        rid = hashlib.sha256(f"{role}\x00{name}".encode()).hexdigest()[:32]
        sql = "SELECT * FROM type::record('entity', $rid) LIMIT 1"
        try:
            results = self.query(sql, {"rid": rid})
        except SurrealError:
            return None
        if not results:
            return None
        row = results[0]
        if isinstance(row, list):
            return row[0] if row else None
        return row if row else None

    def fetch_entity_by_id(self, entity_id: str) -> dict[str, Any] | None:
        """Return a single entity by its SurrealDB record id string, or None.

        *entity_id* must be a valid SurrealDB record id string in
        ``table:record_id`` format (e.g. ``entity:abc123``).
        Uses ``type::record`` (the correct SurrealDB 2.x/3.x function)
        with the full id string as a bound parameter.
        """
        sql = "SELECT * FROM type::record($id) LIMIT 1"
        try:
            results = self.query(sql, {"id": entity_id})
        except SurrealError:
            return None
        if not results:
            return None
        row = results[0]
        if isinstance(row, list):
            return row[0] if row else None
        return row if row else None

    def delete_entity_by_id(self, entity_id: str) -> bool:
        """Delete an entity record by its SurrealDB record id string.

        *entity_id* must be in ``table:record_id`` format (e.g. ``entity:abc123``).
        Returns True if a row was deleted, False otherwise.
        """
        sql = "DELETE type::record($id) RETURN BEFORE"
        try:
            results = self.query(sql, {"id": entity_id})
        except SurrealError:
            return False
        if not results:
            return False
        row = results[0]
        if isinstance(row, list):
            return len(row) > 0
        return bool(row)

    # ── Wiki operations ───────────────────────────────────────────────────────

    def delete_wiki_chunks_by_path(self, path: str) -> int:
        """Delete all wiki_chunk rows for the given parent_path. Returns count deleted."""
        results = self.query(
            "DELETE wiki_chunk WHERE parent_path = $path RETURN BEFORE",
            {"path": path},
        )
        if not results:
            return 0
        row = results[0]
        if isinstance(row, list):
            return len(row)
        return 0

    def upsert_wiki_chunks(
        self,
        chunks: list[dict[str, Any]],
    ) -> None:
        """Upsert a list of wiki_chunk rows.

        Each chunk dict must have: parent_path, heading, chunk_index, text.
        Optional: embedding (list[float]).
        Each chunk gets a deterministic id keyed on (parent_path, chunk_index).
        """
        for chunk in chunks:
            parent_path = chunk["parent_path"]
            chunk_index = int(chunk["chunk_index"])
            rid = hashlib.sha256(
                f"wiki_chunk\x00{parent_path}\x00{chunk_index}".encode()
            ).hexdigest()[:32]
            sql = (
                "UPSERT type::record('wiki_chunk', $rid) SET "
                "parent_path = $parent_path, heading = $heading, "
                "chunk_index = $chunk_index, text = $text, "
                "created_at = time::now()"
            )
            vars: dict[str, Any] = {
                "rid": rid,
                "parent_path": parent_path,
                "heading": chunk.get("heading", ""),
                "chunk_index": chunk_index,
                "text": chunk.get("text", ""),
            }
            embedding = chunk.get("embedding")
            if embedding is not None:
                sql += ", embedding = $embedding"
                vars["embedding"] = embedding
            self.query(sql, vars)

    def recall_wiki_chunks(
        self,
        embedding: list[float],
        limit: int = 10,
        ef: int = 100,
    ) -> list[dict[str, Any]]:
        """Find nearest wiki_chunk rows using the HNSW KNN index.

        Returns rows with parent_path, heading, chunk_index, text, and dist.
        K and EF must be integer literals.
        """
        k = int(limit)
        ef_int = int(ef)
        sql = (
            f"SELECT parent_path, heading, chunk_index, text, "
            f"vector::distance::knn() AS dist FROM wiki_chunk "
            f"WHERE embedding <|{k},{ef_int}|> $vec "
            f"ORDER BY dist"
        )
        results = self.query(sql, {"vec": embedding})
        if not results:
            return []
        row = results[0]
        return row if isinstance(row, list) else []

    def upsert_wiki_doc(
        self,
        title: str,
        body: str,
        frontmatter: dict[str, Any] | None = None,
        path: str = "",
        confidence: float = 1.0,
        summary: str = "",
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Upsert a wiki document."""
        status = "auto_serve" if confidence >= 0.7 else "needs_review"
        rid = hashlib.sha256(f"wiki_doc\x00{path}".encode()).hexdigest()[:32]
        sql = (
            "UPSERT type::record('wiki_doc', $rid) SET "
            "title = $title, body = $body, summary = $summary, frontmatter = $frontmatter, "
            "path = $path, confidence = $confidence, status = $status, "
            "updated_at = time::now()"
        )
        if embedding is not None:
            sql += ", embedding = $embedding"
        vars: dict[str, Any] = {
            "rid": rid,
            "title": title,
            "body": body,
            "summary": summary,
            "frontmatter": frontmatter or {},
            "path": path,
            "confidence": confidence,
            "status": status,
        }
        if embedding is not None:
            vars["embedding"] = embedding
        results = self.query(sql, vars)
        if results and results[0]:
            row = results[0]
            return row[0] if isinstance(row, list) else row
        return {}
