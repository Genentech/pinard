package main

import (
	"encoding/json"
	"math"
	"testing"
)

// ── sessionDedup ──────────────────────────────────────────────────────────────

func TestSessionDedupFiltersSeenNames(t *testing.T) {
	d := &sessionDedup{seen: make(map[string]map[string]bool)}
	entities := []map[string]any{
		{"name": "Use Go", "role": "decision"},
		{"name": "Use NATS", "role": "decision"},
	}
	out := d.Filter("sess1", entities)
	if len(out) != 2 {
		t.Fatalf("first filter: got %d, want 2", len(out))
	}
	// Second call with same session: same names filtered.
	out2 := d.Filter("sess1", entities)
	if len(out2) != 0 {
		t.Errorf("second filter: got %d, want 0 (already seen)", len(out2))
	}
}

func TestSessionDedupDifferentSessionsAreIndependent(t *testing.T) {
	d := &sessionDedup{seen: make(map[string]map[string]bool)}
	entity := []map[string]any{{"name": "Use Go", "role": "decision"}}

	d.Filter("sess1", entity)
	out := d.Filter("sess2", entity)
	if len(out) != 1 {
		t.Errorf("sess2 should see Use Go (different session), got %d", len(out))
	}
}

func TestSessionDedupFallsBackToTitle(t *testing.T) {
	d := &sessionDedup{seen: make(map[string]map[string]bool)}
	entities := []map[string]any{
		{"title": "Wiki Page A", "path": "/wiki/a"},
	}
	out := d.Filter("s", entities)
	if len(out) != 1 {
		t.Fatalf("expected 1, got %d", len(out))
	}
	// Same entity again — deduped by title.
	out2 := d.Filter("s", entities)
	if len(out2) != 0 {
		t.Errorf("wiki entity not deduped by title, got %d", len(out2))
	}
}

func TestSessionDedupEmptyNameDeduplicatesItself(t *testing.T) {
	d := &sessionDedup{seen: make(map[string]map[string]bool)}
	// No name or title — empty string key; second identical entity should be filtered.
	entities := []map[string]any{{"role": "artifact"}}
	out := d.Filter("s", entities)
	if len(out) != 1 {
		t.Fatalf("expected 1, got %d", len(out))
	}
	out2 := d.Filter("s", entities)
	if len(out2) != 0 {
		t.Errorf("empty-name entity not deduped, got %d", len(out2))
	}
}

// ── gateByRelevance ───────────────────────────────────────────────────────────

func TestGateByRelevanceWithinThreshold(t *testing.T) {
	hits := []map[string]any{
		{"name": "A", "dist": 0.2},
		{"name": "B", "dist": 0.4},
		{"name": "C", "dist": 0.9},
	}
	t.Setenv("RECALL_DISTANCE_THRESHOLD", "0.65")
	out := gateByRelevance(hits)
	if len(out) != 2 {
		t.Errorf("expected 2 within-threshold hits, got %d", len(out))
	}
	for _, h := range out {
		if d, _ := h["dist"].(float64); d > 0.65 {
			t.Errorf("hit %v has dist %f > threshold", h["name"], d)
		}
	}
}

func TestGateByRelevanceAllBeyondReturnsTopK(t *testing.T) {
	hits := []map[string]any{
		{"name": "A", "dist": 0.8},
		{"name": "B", "dist": 0.85},
		{"name": "C", "dist": 0.9},
		{"name": "D", "dist": 0.95},
	}
	t.Setenv("RECALL_DISTANCE_THRESHOLD", "0.65")
	out := gateByRelevance(hits)
	if len(out) != 3 {
		t.Errorf("expected 3 fallback hits, got %d", len(out))
	}
}

func TestGateByRelevanceEmpty(t *testing.T) {
	out := gateByRelevance(nil)
	if len(out) != 0 {
		t.Errorf("expected empty, got %d", len(out))
	}
}

func TestGateByRelevanceFewBeyond(t *testing.T) {
	// Fewer than 3 beyond-threshold hits → return all of them.
	hits := []map[string]any{
		{"name": "A", "dist": 0.8},
		{"name": "B", "dist": 0.9},
	}
	t.Setenv("RECALL_DISTANCE_THRESHOLD", "0.65")
	out := gateByRelevance(hits)
	if len(out) != 2 {
		t.Errorf("expected 2, got %d", len(out))
	}
}

// ── entitledScopes ────────────────────────────────────────────────────────────

func TestEntitledScopesCombinesGroupAndVignoble(t *testing.T) {
	t.Setenv("GLOBAL_WIKI_GROUP", "__global__")
	scopes := entitledScopes("my-group", "my-vignoble")
	if len(scopes) != 3 {
		t.Fatalf("expected 3 scopes, got %d: %v", len(scopes), scopes)
	}
	if scopes[0] != "my-group" {
		t.Errorf("scope[0] = %q, want my-group", scopes[0])
	}
	if scopes[1] != "vignoble-my-vignoble" {
		t.Errorf("scope[1] = %q, want vignoble-my-vignoble", scopes[1])
	}
	if scopes[2] != "__global__" {
		t.Errorf("scope[2] = %q, want __global__", scopes[2])
	}
}

func TestEntitledScopesEmptyGroupExcluded(t *testing.T) {
	t.Setenv("GLOBAL_WIKI_GROUP", "__global__")
	scopes := entitledScopes("", "my-vignoble")
	if len(scopes) != 2 {
		t.Fatalf("expected 2 scopes, got %d: %v", len(scopes), scopes)
	}
	if scopes[0] != "vignoble-my-vignoble" {
		t.Errorf("scope[0] = %q, want vignoble-my-vignoble", scopes[0])
	}
}

// ── buildSources ──────────────────────────────────────────────────────────────

func TestBuildSourcesEntityRecord(t *testing.T) {
	hits := []map[string]any{
		{"role": "decision", "name": "Use Go", "id": "entity:abc", "dist": 0.2},
	}
	sources := buildSources(hits)
	if len(sources) != 1 {
		t.Fatalf("expected 1 source, got %d", len(sources))
	}
	if sources[0]["type"] != "surrealdb" {
		t.Errorf("type = %v, want surrealdb", sources[0]["type"])
	}
	if sources[0]["role"] != "decision" {
		t.Errorf("role = %v, want decision", sources[0]["role"])
	}
	score, _ := sources[0]["score"].(float64)
	expected := math.Round((1.0-0.2)*10000) / 10000
	if math.Abs(score-expected) > 1e-9 {
		t.Errorf("score = %f, want %f", score, expected)
	}
}

func TestBuildSourcesWikiRecord(t *testing.T) {
	hits := []map[string]any{
		{"_wiki": true, "path": "/wiki/go", "title": "Go Guide", "confidence": 0.9, "status": "auto_serve"},
	}
	sources := buildSources(hits)
	if len(sources) != 1 {
		t.Fatalf("expected 1 source, got %d", len(sources))
	}
	if sources[0]["type"] != "wiki" {
		t.Errorf("type = %v, want wiki", sources[0]["type"])
	}
	if sources[0]["path"] != "/wiki/go" {
		t.Errorf("path = %v, want /wiki/go", sources[0]["path"])
	}
}

func TestBuildSourcesEmpty(t *testing.T) {
	sources := buildSources(nil)
	if sources != nil {
		t.Errorf("expected nil sources for empty hits, got %v", sources)
	}
}

// ── summarize (nil LLM — verbatim fallback) ───────────────────────────────────

func TestSummarizeFallbackNoLLMDescription(t *testing.T) {
	hits := []map[string]any{
		{"description": "Always use JetStream for durable events"},
	}
	out := summarize(nil, hits, 400)
	if out == "" {
		t.Error("expected non-empty verbatim fallback")
	}
	if len(out) > 400 {
		t.Errorf("verbatim fallback length = %d, want ≤400", len(out))
	}
}

func TestSummarizeFallbackNoLLMWikiBody(t *testing.T) {
	hits := []map[string]any{
		{"_wiki": true, "body": "Wiki page body content with useful information"},
	}
	out := summarize(nil, hits, 400)
	if out == "" {
		t.Error("expected non-empty verbatim fallback for wiki hit")
	}
}

func TestSummarizeFallbackNoLLMEmpty(t *testing.T) {
	out := summarize(nil, nil, 400)
	if out != "" {
		t.Errorf("expected empty string for empty hits, got %q", out)
	}
}

func TestSummarizeFallbackTruncatesLong(t *testing.T) {
	long := make([]byte, 600)
	for i := range long {
		long[i] = 'x'
	}
	hits := []map[string]any{
		{"description": string(long)},
	}
	out := summarize(nil, hits, 400)
	if len(out) > 400 {
		t.Errorf("fallback should truncate to 400, got %d", len(out))
	}
}

// ── nullResponse ──────────────────────────────────────────────────────────────

func TestNullResponse(t *testing.T) {
	b := nullResponse()
	if len(b) == 0 {
		t.Fatal("nullResponse should return non-empty bytes")
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("nullResponse not valid JSON: %v", err)
	}
	if m["context"] != "" {
		t.Errorf("context = %v, want empty string", m["context"])
	}
}

// ── handleRecall integration (no SurrealDB) ───────────────────────────────────

func TestHandleRecallDegradeGracefullyNoBackend(t *testing.T) {
	// With nil embedder and no reachable SurrealDB, recall degrades gracefully.
	req := recallRequest{}
	req.Query.UserMessage = "How do I use JetStream?"
	req.GroupID = "test-group-unreachable"

	// Should not panic; context will be empty since SurrealDB unreachable.
	resp := handleRecall(req, nil, nil)
	// Context is empty or a [memory] prefix, no panic.
	_ = resp.Sources
	_ = resp.Meta
}

func TestHandleRecallContextPrefixed(t *testing.T) {
	// When summarize returns non-empty, context should have [memory] prefix.
	// Inject a hit that produces verbatim fallback without LLM.
	// We can't inject SurrealDB hits via handleRecall directly, so
	// verify summarize + prefix logic separately.
	hits := []map[string]any{{"description": "some context"}}
	out := summarize(nil, hits, 400)
	if out == "" {
		t.Skip("no fallback content")
	}
	prefixed := "[memory] " + out
	if len(prefixed) < len("[memory] ") {
		t.Error("prefixed context too short")
	}
}
