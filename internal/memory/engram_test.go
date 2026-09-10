package memory

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestEngramReaderFetch(t *testing.T) {
	obs := []map[string]any{
		{
			"id":         "obs1",
			"session_id": "s1",
			"project":    "test-group",
			"type":       "rule",
			"content":    "Use Go for new services",
			"created_at": time.Now().UTC().Format(time.RFC3339),
			"confidence": 0.9,
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/observations" {
			http.Error(w, "not found", 404)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(obs)
	}))
	defer srv.Close()

	reader := &EngramReader{
		GroupID:    "test-group",
		URL:        srv.URL,
		SinceHours: 168,
		hc:         &http.Client{},
	}
	results, err := reader.Fetch()
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}
	if results[0].ObsType != "rule" {
		t.Errorf("obs_type = %q, want rule", results[0].ObsType)
	}
	if results[0].Confidence != 0.9 {
		t.Errorf("confidence = %f, want 0.9", results[0].Confidence)
	}
}

func TestEngramReaderHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "bad endpoint", 404)
	}))
	defer srv.Close()

	reader := &EngramReader{
		GroupID:    "test-group",
		URL:        srv.URL,
		SinceHours: 168,
		hc:         &http.Client{},
	}
	_, err := reader.Fetch()
	if err == nil {
		t.Fatal("expected error on 404")
	}
}

func TestParseEngramItem(t *testing.T) {
	m := map[string]any{
		"id":         "obs42",
		"session_id": "sess1",
		"type":       "decision",
		"content":    "Always use JetStream for durable events",
		"created_at": "2025-01-15T10:00:00Z",
		"confidence": 0.85,
	}
	obs := parseEngramItem(m, "default-group")
	if obs.ObsID != "obs42" {
		t.Errorf("ObsID = %q", obs.ObsID)
	}
	if obs.ObsType != "decision" {
		t.Errorf("ObsType = %q", obs.ObsType)
	}
	if obs.GroupID != "default-group" {
		t.Errorf("GroupID = %q", obs.GroupID)
	}
	if obs.Confidence != 0.85 {
		t.Errorf("Confidence = %f", obs.Confidence)
	}
}

func TestParseEngramItemGroupIDFromPayload(t *testing.T) {
	m := map[string]any{
		"project": "explicit-group",
		"type":    "fact",
		"content": "some fact",
	}
	obs := parseEngramItem(m, "default-group")
	if obs.GroupID != "explicit-group" {
		t.Errorf("GroupID = %q, want explicit-group", obs.GroupID)
	}
}

func TestParseEngramItemDefaultObsType(t *testing.T) {
	m := map[string]any{"content": "x"}
	obs := parseEngramItem(m, "g")
	if obs.ObsType != "fact" {
		t.Errorf("ObsType = %q, want fact", obs.ObsType)
	}
}
