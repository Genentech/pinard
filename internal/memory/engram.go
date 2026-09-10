// Package memory provides the Go implementations of the pinard memory layer
// services: Engram readers, LLM client, embedding client, and ingestion logic.
package memory

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// EngramObservation is a single curated observation from Engram.
type EngramObservation struct {
	ObsID     string
	SessionID string
	GroupID   string
	ObsType   string // "rule", "fact", "teaching-episode", "summary", …
	Content   string
	Timestamp time.Time
	Confidence float64
	Metadata  map[string]any
}

// EngramReaderError is returned on HTTP or parse errors from the Engram API.
type EngramReaderError struct {
	Message string
}

func (e *EngramReaderError) Error() string { return "engram: " + e.Message }

// EngramReader reads curated observations from a local Engram HTTP API.
type EngramReader struct {
	GroupID    string
	URL        string
	APIKey     string
	SinceHours int
	hc         *http.Client
}

// NewEngramReader creates an EngramReader using environment defaults.
//
//	ENGRAM_URL        — base URL (default: http://localhost:7783)
//	ENGRAM_API_KEY    — optional bearer token
//	ENGRAM_SINCE_HOURS — look-back window in hours (default: 168)
func NewEngramReader(groupID string) *EngramReader {
	sinceHours := 168
	if s := os.Getenv("ENGRAM_SINCE_HOURS"); s != "" {
		if n, err := strconv.Atoi(s); err == nil {
			sinceHours = n
		}
	}
	return &EngramReader{
		GroupID:    groupID,
		URL:        strings.TrimRight(envOr("ENGRAM_URL", "http://localhost:7783"), "/"),
		APIKey:     os.Getenv("ENGRAM_API_KEY"),
		SinceHours: sinceHours,
		hc:         &http.Client{Timeout: 30 * time.Second},
	}
}

// Fetch returns curated observations for the reader's GroupID.
func (r *EngramReader) Fetch() ([]EngramObservation, error) {
	params := url.Values{}
	params.Set("project", r.GroupID)
	params.Set("since_hours", strconv.Itoa(r.SinceHours))
	params.Set("limit", "500")

	endpoint := r.URL + "/observations?" + params.Encode()
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, &EngramReaderError{Message: fmt.Sprintf("build request: %v", err)}
	}
	req.Header.Set("Accept", "application/json")
	if r.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+r.APIKey)
	}

	resp, err := r.hc.Do(req)
	if err != nil {
		return nil, &EngramReaderError{Message: fmt.Sprintf("connection failed: %v", err)}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		return nil, &EngramReaderError{Message: fmt.Sprintf("HTTP %d — possible endpoint misconfiguration: %s", resp.StatusCode, string(body)[:min(300, len(body))])}
	}

	// Body is either a JSON array or {"observations": [...]}
	var raw any
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, &EngramReaderError{Message: fmt.Sprintf("invalid JSON: %v", err)}
	}

	var items []any
	switch t := raw.(type) {
	case []any:
		items = t
	case map[string]any:
		if obs, ok := t["observations"].([]any); ok {
			items = obs
		}
	}

	out := make([]EngramObservation, 0, len(items))
	for _, item := range items {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		obs := parseEngramItem(m, r.GroupID)
		out = append(out, obs)
	}
	return out, nil
}

func parseEngramItem(m map[string]any, defaultGroupID string) EngramObservation {
	ts := time.Now().UTC()
	if rawTS, ok := m["created_at"].(string); ok && rawTS != "" {
		rawTS = strings.Replace(rawTS, "Z", "+00:00", 1)
		if t, err := time.Parse(time.RFC3339, rawTS); err == nil {
			ts = t
		}
	} else if rawTS, ok := m["timestamp"].(string); ok && rawTS != "" {
		rawTS = strings.Replace(rawTS, "Z", "+00:00", 1)
		if t, err := time.Parse(time.RFC3339, rawTS); err == nil {
			ts = t
		}
	}

	confidence := 1.0
	if c, ok := m["confidence"].(float64); ok {
		confidence = c
	}

	meta := map[string]any{}
	if mv, ok := m["metadata"].(map[string]any); ok {
		meta = mv
	}

	return EngramObservation{
		ObsID:      strVal(m, "id", "obs_id"),
		SessionID:  strVal(m, "session_id"),
		GroupID:    strValOr(strVal(m, "project", "group_id"), defaultGroupID),
		ObsType:    strValOr(strVal(m, "type", "obs_type"), "fact"),
		Content:    strVal(m, "content", "body"),
		Timestamp:  ts,
		Confidence: confidence,
		Metadata:   meta,
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func strVal(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return s
			}
		}
	}
	return ""
}

func strValOr(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
