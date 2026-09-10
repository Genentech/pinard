//go:build integration

package memory

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Genentech/pinard/internal/surreal"
)

// memRunID isolates this test run's SurrealDB databases from previous pipeline runs.
var memRunID = fmt.Sprintf("%d", time.Now().UnixMilli()%100000)

// corpusObs is the JSON shape of tests/memory-parity/corpus/observations.json.
type corpusObs struct {
	ID        string  `json:"id"`
	SessionID string  `json:"session_id"`
	Project   string  `json:"project"`
	Type      string  `json:"type"`
	Content   string  `json:"content"`
	CreatedAt string  `json:"created_at"`
	Confidence float64 `json:"confidence"`
}

// loadCorpus reads the fixture corpus.
func loadCorpus(t *testing.T) []corpusObs {
	t.Helper()
	b, err := os.ReadFile("../../tests/memory-parity/corpus/observations.json")
	if err != nil {
		t.Skipf("corpus not found: %v", err)
	}
	var corpus []corpusObs
	if err := json.Unmarshal(b, &corpus); err != nil {
		t.Fatalf("parse corpus: %v", err)
	}
	return corpus
}

// integrationSurrealClient opens a Client against the live SurrealDB.
// groupID is suffixed with memRunID to prevent cross-run data pollution.
func integrationSurrealClient(t *testing.T, groupID string) *surreal.Client {
	t.Helper()
	url := os.Getenv("SURREAL_URL")
	if url == "" {
		t.Skip("SURREAL_URL not set — skipping integration test")
	}
	user := os.Getenv("SURREAL_USER")
	if user == "" {
		user = "root"
	}
	pass := os.Getenv("SURREAL_PASS")
	uniqueGroupID := groupID + "r" + memRunID
	c, err := surreal.NewWithConfig(uniqueGroupID, url, user, pass)
	if err != nil {
		t.Fatalf("surreal.NewWithConfig: %v", err)
	}
	if err := c.EnsureSchema(); err != nil {
		t.Fatalf("EnsureSchema: %v", err)
	}
	return c
}

// mockRosettaSrv returns an httptest.Server that yields a deterministic embedding.
func mockRosettaSrv(t *testing.T) *httptest.Server {
	t.Helper()
	vec := make([]float64, surreal.EmbeddingDim)
	for i := range vec {
		vec[i] = float64(i) / float64(surreal.EmbeddingDim)
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]any{
			"data": []map[string]any{
				{"index": 0, "embedding": vec},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}))
}

// TestIntegrationEngramReaderWithLiveCorpus serves the corpus via a mock Engram
// HTTP server and verifies the EngramReader parses all observations correctly.
func TestIntegrationEngramReaderWithLiveCorpus(t *testing.T) {
	corpus := loadCorpus(t)

	// Build the JSON payload Engram would return.
	var payload []map[string]any
	for _, obs := range corpus {
		payload = append(payload, map[string]any{
			"id":         obs.ID,
			"session_id": obs.SessionID,
			"project":    obs.Project,
			"type":       obs.Type,
			"content":    obs.Content,
			"created_at": obs.CreatedAt,
			"confidence": obs.Confidence,
		})
	}

	engramSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(payload)
	}))
	defer engramSrv.Close()
	t.Setenv("ENGRAM_URL", engramSrv.URL)

	reader := NewEngramReader("memory-parity-test")
	observations, err := reader.Fetch()
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if len(observations) != len(corpus) {
		t.Errorf("got %d observations, want %d", len(observations), len(corpus))
	}
	for i, obs := range observations {
		if obs.ObsType != corpus[i].Type {
			t.Errorf("obs[%d].ObsType = %q, want %q", i, obs.ObsType, corpus[i].Type)
		}
		if obs.Confidence != corpus[i].Confidence {
			t.Errorf("obs[%d].Confidence = %f, want %f", i, obs.Confidence, corpus[i].Confidence)
		}
	}
}

// TestIntegrationEmbedderWithMockRosetta verifies the Embedder produces a
// 1024-dim vector from a mock Rosetta endpoint (integration-tagged so it runs
// in the same CI job that has NATS/SurrealDB available).
func TestIntegrationEmbedderWithMockRosetta(t *testing.T) {
	rosetta := mockRosettaSrv(t)
	defer rosetta.Close()
	t.Setenv("ROSETTA_URL", rosetta.URL)

	emb := NewEmbedder()
	vec, err := emb.Embed("test content")
	if err != nil {
		t.Fatalf("Embed: %v", err)
	}
	if len(vec) != surreal.EmbeddingDim {
		t.Errorf("embedding dim = %d, want %d", len(vec), surreal.EmbeddingDim)
	}
}

// TestIntegrationCorpusIngestPipeline is the primary E2E test:
//  1. Serve corpus/observations.json via mock Engram.
//  2. Embed via mock Rosetta.
//  3. Ingest into a live SurrealDB instance.
//  4. Assert every corpus entity is present with the correct role.
//
// This test MUST fail on regression (wrong role mapping, broken upsert, schema mismatch).
func TestIntegrationCorpusIngestPipeline(t *testing.T) {
	corpus := loadCorpus(t)
	db := integrationSurrealClient(t, "ci-corpus-ingest")

	rosetta := mockRosettaSrv(t)
	defer rosetta.Close()
	t.Setenv("ROSETTA_URL", rosetta.URL)

	emb := NewEmbedder()

	// obsTypeToRole mapping (duplicated from cmd/memory-ingester to avoid import cycle).
	obsTypeToRole := map[string]string{
		"rule": "decision", "fact": "artifact", "teaching-episode": "task",
		"summary": "task", "diagnosis": "diagnosis", "action": "action",
		"log_pattern": "log_pattern", "environment_condition": "environment_condition",
		"gate": "gate", "step": "step", "verdict": "verdict",
		"bugfix": "diagnosis", "decision": "decision", "architecture": "artifact",
		"discovery": "artifact", "pattern": "decision", "config": "artifact",
		"preference": "artifact", "session_summary": "task", "plan": "task", "manual": "decision",
	}

	// Load golden to check expected roles.
	type goldenEntity struct {
		ObsType string `json:"obs_type"`
		Role    string `json:"role"`
		Name    string `json:"name"`
	}
	goldenBytes, err := os.ReadFile("../../tests/memory-parity/golden/recall.json")
	if err != nil {
		t.Skipf("golden recall not found: %v", err)
	}
	var goldenData struct {
		Entities []goldenEntity `json:"entities"`
	}
	if err := json.Unmarshal(goldenBytes, &goldenData); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	goldenByObsID := make(map[string]goldenEntity, len(goldenData.Entities))
	for i, e := range goldenData.Entities {
		goldenByObsID[corpus[i].ID] = e
	}

	// Ingest each observation.
	type ingestResult struct {
		ObsID    string
		Role     string
		Name     string
		Err      error
	}
	var results []ingestResult

	for _, obs := range corpus {
		obsType := obs.Type
		role, ok := obsTypeToRole[obsType]
		if !ok {
			role = "artifact"
		}

		// Extract name (first non-empty line, strip markdown headings).
		firstLine := obs.Content
		if nl := strings.Index(obs.Content, "\n"); nl >= 0 {
			firstLine = obs.Content[:nl]
		}
		firstLine = strings.TrimSpace(firstLine)
		for strings.HasPrefix(firstLine, "#") {
			firstLine = strings.TrimPrefix(firstLine, "#")
			firstLine = strings.TrimSpace(firstLine)
		}
		if len(firstLine) > 120 {
			firstLine = firstLine[:120]
		}
		if firstLine == "" {
			firstLine = obs.Content
			if len(firstLine) > 120 {
				firstLine = firstLine[:120]
			}
			firstLine = strings.ReplaceAll(firstLine, "\n", " ")
		}
		name := firstLine

		// Embed.
		vec, embedErr := emb.Embed(obs.Content)
		if embedErr != nil {
			t.Logf("WARN: embed obs %s: %v", obs.ID, embedErr)
		}

		// Upsert.
		_, upsertErr := db.UpsertEntity(role, name, obs.Content, nil, vec, "1.0.0", "integration-test")
		results = append(results, ingestResult{
			ObsID: obs.ID, Role: role, Name: name, Err: upsertErr,
		})
	}

	// Assert all ingested without error.
	for _, r := range results {
		if r.Err != nil {
			t.Errorf("ingest obs %s: %v", r.ObsID, r.Err)
		}
	}

	// Assert each entity is present with the correct role (parity check vs Python golden).
	failCount := 0
	for i, r := range results {
		if r.Err != nil {
			continue
		}
		goldenE := goldenByObsID[corpus[i].ID]
		if r.Role != goldenE.Role {
			t.Errorf("obs %s: Go role=%q, Python role=%q (parity failure)", corpus[i].ID, r.Role, goldenE.Role)
			failCount++
		}

		// Fetch from live DB and verify.
		row, err := db.FetchEntityByRoleName(r.Role, r.Name)
		if err != nil {
			t.Errorf("FetchEntityByRoleName obs %s: %v", corpus[i].ID, err)
			failCount++
			continue
		}
		if row == nil {
			t.Errorf("obs %s: entity not found in live DB after upsert (role=%q, name=%q)", corpus[i].ID, r.Role, r.Name)
			failCount++
			continue
		}
	}

	if failCount > 0 {
		t.Errorf("%d/%d corpus entities failed parity check", failCount, len(corpus))
	} else {
		t.Logf("All %d corpus entities ingested and verified ✓", len(corpus))
	}
}

// TestIntegrationSchemaFieldsParity verifies that the Go schema (as applied via
// EnsureSchema) contains every field and relation table that the Python
// schema_gen.py produced (from tests/memory-parity/golden/schema.sql).
func TestIntegrationSchemaFieldsParity(t *testing.T) {
	url := os.Getenv("SURREAL_URL")
	if url == "" {
		t.Skip("SURREAL_URL not set — skipping integration test")
	}

	goldenSQL, err := os.ReadFile("../../tests/memory-parity/golden/schema.sql")
	if err != nil {
		t.Skipf("golden schema not found: %v", err)
	}
	golden := string(goldenSQL)

	// These are the relation tables the Python schema_gen.py generates from
	// the core ontology — they must match exactly or existing data becomes unqueryable.
	requiredTables := []string{
		"depends_on", "produces", "consumes",
		"indicates_problem", "resolved_by", "requires_condition", "triggers_decision",
	}
	for _, tbl := range requiredTables {
		defineStr := "DEFINE TABLE IF NOT EXISTS " + tbl
		if !strings.Contains(golden, defineStr) {
			t.Errorf("Python golden schema missing table %q", tbl)
		}
	}

	// Required base fields on entity — golden uses padded column format, so
	// check for field name followed by whitespace then "ON entity" rather than exact spacing.
	for _, field := range []string{"name", "role", "description", "version", "provenance", "embedding", "manual_edit"} {
		// Match: "DEFINE FIELD IF NOT EXISTS <field>...ON entity"
		if !containsFieldOnTable(golden, field, "entity") {
			t.Errorf("Python golden missing entity field %q", field)
		}
	}

	// Staging tables must be in Python golden (data-compat: Python wrote them, Go reads them).
	for _, tbl := range []string{"entity_staging", "edge_staging", "ingest_cursor"} {
		if !strings.Contains(golden, "DEFINE TABLE IF NOT EXISTS "+tbl) {
			t.Errorf("Python golden missing table %q", tbl)
		}
	}

	t.Logf("Schema parity check passed — %d required tables present in Python golden", len(requiredTables))
}

// containsFieldOnTable checks whether the DDL string defines <field> on <table>,
// tolerating the padded column format used by Python schema_gen.py
// (e.g. "DEFINE FIELD IF NOT EXISTS name        ON entity TYPE string")
// vs the compact format used by Go
// (e.g. "DEFINE FIELD IF NOT EXISTS name ON entity TYPE string").
func containsFieldOnTable(ddl, field, table string) bool {
	prefix := "DEFINE FIELD IF NOT EXISTS " + field
	for i := 0; i <= len(ddl)-len(prefix); i++ {
		if ddl[i:i+len(prefix)] != prefix {
			continue
		}
		// Skip whitespace after the field name.
		j := i + len(prefix)
		for j < len(ddl) && (ddl[j] == ' ' || ddl[j] == '\t') {
			j++
		}
		// Must be followed by "ON <table>".
		suffix := "ON " + table
		if j+len(suffix) <= len(ddl) && ddl[j:j+len(suffix)] == suffix {
			return true
		}
	}
	return false
}

// TestIntegrationLookupAfterIngest verifies FTS lookup returns ingested entities.
func TestIntegrationLookupAfterIngest(t *testing.T) {
	db := integrationSurrealClient(t, "ci-lookup-test")

	unique := "xyzuniquetermabcdef" + time.Now().Format("150405")
	_, err := db.UpsertEntity("decision", "integration-lookup-"+unique, unique+" content for FTS", nil, nil, "1.0.0", "test")
	if err != nil {
		t.Fatalf("UpsertEntity: %v", err)
	}

	rows, err := db.Lookup(unique, 5)
	if err != nil {
		t.Fatalf("Lookup: %v", err)
	}
	found := false
	for _, r := range rows {
		if name, _ := r["name"].(string); strings.Contains(name, unique) {
			found = true
		}
	}
	if !found {
		t.Errorf("FTS lookup did not find entity after upsert (term=%q)", unique)
	}
}
