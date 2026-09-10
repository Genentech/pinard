// Command memory-recall is the Go port of services/memory/recall_service.py.
//
// Subscribes to `pinard.{vignoble}.recall` (core NATS request-reply) and
// responds with summarized knowledge drawn from SurrealDB.
//
// Environment variables:
//
//	NATS_URL         — NATS server URL (default: nats://localhost:4222)
//	NATS_VIGNOBLE    — vignoble name (required)
//	SURREAL_URL      — SurrealDB endpoint
//	SURREAL_USER     — SurrealDB username
//	SURREAL_PASS     — SurrealDB password
//	ROSETTA_URL      — Rosetta embedding endpoint
//	MEMORY_LLM_*     — LLM client config (see internal/memory/llm.go)
//	RECALL_DISTANCE_THRESHOLD — cosine distance threshold (default: 0.65)
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/Genentech/pinard/internal/memory"
	"github.com/Genentech/pinard/internal/surreal"
	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

func main() {
	root := &cobra.Command{
		Use:   "memory-recall",
		Short: "Pinard memory recall service — NATS request-reply over SurrealDB",
		RunE:  run,
	}
	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

func run(cmd *cobra.Command, args []string) error {
	vignoble := os.Getenv("NATS_VIGNOBLE")
	if vignoble == "" {
		return fmt.Errorf("NATS_VIGNOBLE is required")
	}
	return runRecall(vignoble)
}

// ── Session dedup ─────────────────────────────────────────────────────────────

// sessionDedup tracks entity names already sent per session_id.
type sessionDedup struct {
	mu   sync.Mutex
	seen map[string]map[string]bool // session_id → set of entity names
}

var globalDedup = &sessionDedup{seen: make(map[string]map[string]bool)}

func (d *sessionDedup) Filter(sessionID string, entities []map[string]any) []map[string]any {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.seen[sessionID]; !ok {
		d.seen[sessionID] = make(map[string]bool)
	}
	seen := d.seen[sessionID]
	var out []map[string]any
	for _, e := range entities {
		name, _ := e["name"].(string)
		if name == "" {
			name, _ = e["title"].(string)
		}
		if !seen[name] {
			seen[name] = true
			out = append(out, e)
		}
	}
	return out
}

// ── Recall request/response ────────────────────────────────────────────────

// recallRequest is the NATS request payload.
type recallRequest struct {
	SessionID string `json:"session_id"`
	GroupID   string `json:"group_id"`
	Vignoble  string `json:"vignoble"`
	Query     struct {
		UserMessage       string `json:"user_message"`
		AssistantExcerpt  string `json:"assistant_excerpt"`
		TurnIndex         int    `json:"turn_index"`
	} `json:"query"`
	Constraints struct {
		MaxContextTokens int    `json:"max_context_tokens"`
		ExcludeSession   string `json:"exclude_session"`
	} `json:"constraints"`
}

// recallResponse is the NATS reply payload.
type recallResponse struct {
	Context string           `json:"context"`
	Sources []map[string]any `json:"sources"`
	Meta    map[string]any   `json:"meta"`
}

func nullResponse() []byte {
	b, _ := json.Marshal(recallResponse{Context: ""})
	return b
}

// ── Boot-recall request/response ─────────────────────────────────────────────

// bootRecallRequest is the NATS payload for pinard.<v>.recall.boot.
// Matches the Python handle_boot_message payload and the Go client in cmd_memory_boot.go.
type bootRecallRequest struct {
	Scopes   []string `json:"scopes"`
	GroupID  string   `json:"group_id"`
	TaskText string   `json:"task_text"`
	TopK     int      `json:"top_k"`
}

// bootEntry is one compact manifest row: {scope, type, title, summary, ref}.
type bootEntry struct {
	Scope   string `json:"scope"`
	Type    string `json:"type"`
	Title   string `json:"title"`
	Summary string `json:"summary"`
	Ref     string `json:"ref"`
}

// bootRecallResponse is the NATS reply payload for boot-recall.
type bootRecallResponse struct {
	Entries []bootEntry `json:"entries"`
	Meta    struct {
		TotalEntries int `json:"total_entries"`
	} `json:"meta"`
}

func nullBootResponse() []byte {
	var r bootRecallResponse
	r.Entries = []bootEntry{}
	b, _ := json.Marshal(r)
	return b
}

// typedEntityRoles is the set of entity roles surfaced at the vigne tier.
// Excludes raw "artifact" (matches Python _TYPED_ENTITY_ROLES).
var typedEntityRoles = map[string]bool{
	"decision":    true,
	"diagnosis":   true,
	"gotcha":      true,
	"action":      true,
	"log_pattern": true,
	"concept":     true,
	"requirement": true,
	"hypothesis":  true,
}

// stripEntityNoise strips mem_save section labels (What:, **Why**:, etc.) and
// surrounding bold/italic wrappers from text. Used to normalise before dedup.
func stripEntityNoise(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return text
	}
	// Strip section labels iteratively until stable.
	for {
		prev := text
		text = sectionLabelRE.ReplaceAllString(text, "")
		text = strings.TrimSpace(text)
		if text == prev {
			break
		}
	}
	// Strip surrounding **bold** or *italic* wrappers.
	if m := boldWrapRE.FindStringSubmatch(text); m != nil {
		text = strings.TrimSpace(m[1])
	} else if m := boldPrefixRE.FindStringSubmatch(text); m != nil && len(m[0]) == len(text) {
		text = strings.TrimSpace(m[1])
	}
	return text
}

// nullSummaryIfDuplicate returns summary unchanged unless it duplicates or
// prefixes/is-prefixed-by the title (case-insensitive, first 60 chars),
// in which case it returns "" so the manifest entry omits a redundant summary.
func nullSummaryIfDuplicate(title, summary string) string {
	if summary == "" {
		return ""
	}
	tl := strings.ToLower(title)
	sl := strings.ToLower(summary)
	tl60 := tl
	if len(tl60) > 60 {
		tl60 = tl60[:60]
	}
	sl60 := sl
	if len(sl60) > 60 {
		sl60 = sl60[:60]
	}
	if sl60 == tl60 || strings.HasPrefix(sl60, tl60) || strings.HasPrefix(tl60, sl60) {
		return ""
	}
	return summary
}

// cleanEntityTitle deterministically cleans a typed-entity name for use as a
// boot-manifest title (port of Python _clean_entity_title).
func cleanEntityTitle(name string, maxLen int) string {
	title := strings.TrimSpace(name)
	for {
		prev := title
		title = headingMarkRE.ReplaceAllString(title, "")
		title = strings.TrimSpace(title)
		title = sectionLabelRE.ReplaceAllString(title, "")
		title = strings.TrimSpace(title)
		if m := boldWrapRE.FindStringSubmatch(title); m != nil {
			title = strings.TrimSpace(m[1])
		}
		title = strings.TrimRight(title, ":")
		title = strings.TrimSpace(title)
		title = provenanceRE.ReplaceAllString(title, "")
		title = strings.TrimSpace(title)
		if title == prev {
			break
		}
	}
	if maxLen > 0 && len(title) > maxLen {
		title = strings.TrimSpace(title[:maxLen])
	}
	if title == "" {
		return strings.TrimSpace(name)
	}
	return title
}

// ── Boot-recall regex helpers ────────────────────────────────────────────────
//
// These mirror Python's module-level compiled regexes in recall_service.py.

var (
	provenanceRE  = regexp.MustCompile(`\s*\([^)]*\)\s*$`)
	headingMarkRE = regexp.MustCompile(`^#+\s*`)
	sectionLabelRE = regexp.MustCompile(`(?i)^(?:\*{1,2})?(?:What|Why|Where|Learned|How|Note|Result|Context)(?:\*{1,2})?\s*:\.?\s*`)
	boldWrapRE    = regexp.MustCompile(`^\*{1,2}(.+?)\*{1,2}$`)
	boldPrefixRE  = regexp.MustCompile(`^\*{1,2}([^*]+?)\*{1,2}\s*:?\s*`)
)

// ── Scoped SurrealDB query ────────────────────────────────────────────────────

func entitledScopes(groupID, vignoble string) []string {
	globalWikiGroup := envOr("GLOBAL_WIKI_GROUP", "__global__")
	var scopes []string
	if groupID != "" {
		scopes = append(scopes, groupID)
	}
	if vignoble != "" {
		scopes = append(scopes, "vignoble-"+vignoble)
	}
	scopes = append(scopes, globalWikiGroup)
	return scopes
}

func queryScope(scope, queryText string, embedding []float64, seenEntities map[string]bool, seenWiki map[string]bool) []map[string]any {
	db, err := surreal.New(scope)
	if err != nil {
		return nil
	}
	defer db.Close()
	if err := db.EnsureSchema(); err != nil {
		return nil
	}

	var hits []map[string]any

	// Vector recall.
	if embedding != nil {
		rows, err := db.Recall(embedding, 10, "")
		if err == nil {
			for _, r := range rows {
				key := fmt.Sprintf("%v:%v", r["role"], r["name"])
				if !seenEntities[key] {
					seenEntities[key] = true
					r["_scope"] = scope
					hits = append(hits, r)
				}
			}
		}
	}

	// FTS lookup.
	rows, err := db.Lookup(queryText, 5)
	if err == nil {
		for _, r := range rows {
			key := fmt.Sprintf("%v:%v", r["role"], r["name"])
			if !seenEntities[key] {
				seenEntities[key] = true
				r["_scope"] = scope
				hits = append(hits, r)
			}
		}
	}

	// Wiki recall.
	if embedding != nil {
		wikiRows, err := db.RecallWiki(embedding, 5, false)
		if err == nil {
			for _, r := range wikiRows {
				path, _ := r["path"].(string)
				if !seenWiki[path] {
					seenWiki[path] = true
					r["_wiki"] = true
					r["_scope"] = scope
					hits = append(hits, r)
				}
			}
		}
	}

	// Wiki FTS lookup.
	wikiRows, err := db.LookupWiki(queryText, 3, false)
	if err == nil {
		for _, r := range wikiRows {
			path, _ := r["path"].(string)
			if !seenWiki[path] {
				seenWiki[path] = true
				r["_wiki"] = true
				r["_scope"] = scope
				hits = append(hits, r)
			}
		}
	}

	return hits
}

// ── Relevance gating ──────────────────────────────────────────────────────────

func gateByRelevance(hits []map[string]any) []map[string]any {
	threshold := 0.65 // cosine distance threshold (configurable via env)
	if s := os.Getenv("RECALL_DISTANCE_THRESHOLD"); s != "" {
		var f float64
		if _, err := fmt.Sscanf(s, "%f", &f); err == nil {
			threshold = f
		}
	}

	var within, beyond []map[string]any
	for _, h := range hits {
		dist, _ := h["dist"].(float64)
		if dist <= threshold {
			within = append(within, h)
		} else {
			beyond = append(beyond, h)
		}
	}
	if len(within) > 0 {
		return within
	}
	// Return top-k anyway so recall never yields empty.
	if len(beyond) > 3 {
		return beyond[:3]
	}
	return beyond
}

// ── Summarization ─────────────────────────────────────────────────────────────

func summarize(llm *memory.LLMClient, hits []map[string]any, maxTokens int) string {
	if len(hits) == 0 {
		return ""
	}
	if llm == nil {
		// Degrade to verbatim top hit.
		for _, h := range hits {
			if d, ok := h["description"].(string); ok && d != "" {
				if len(d) > 400 {
					d = d[:400]
				}
				return d
			}
			if b, ok := h["body"].(string); ok && b != "" {
				if len(b) > 400 {
					b = b[:400]
				}
				return b
			}
		}
		return ""
	}

	var parts []string
	for _, h := range hits {
		if isWiki, _ := h["_wiki"].(bool); isWiki {
			title, _ := h["title"].(string)
			body, _ := h["body"].(string)
			if len(body) > 800 {
				body = body[:800]
			}
			parts = append(parts, fmt.Sprintf("[wiki: %s]\n%s", title, body))
		} else {
			name, _ := h["name"].(string)
			desc, _ := h["description"].(string)
			if len(desc) > 600 {
				desc = desc[:600]
			}
			parts = append(parts, fmt.Sprintf("[%s]\n%s", name, desc))
		}
	}
	combined := strings.Join(parts, "\n\n")

	text, err := llm.Complete([]memory.LLMMessage{
		{Role: "user", Content: "Summarize the following knowledge in ≤400 tokens for context injection:\n\n" + combined},
	}, maxTokens, "You are a concise technical summarizer.")
	if err != nil {
		log.Printf("WARN: summarize LLM error: %v", err)
		return combined[:int(math.Min(float64(len(combined)), 400))]
	}
	return text
}

// ── Sources builder ───────────────────────────────────────────────────────────

func buildSources(hits []map[string]any) []map[string]any {
	var sources []map[string]any
	for _, h := range hits {
		s := map[string]any{}
		if isWiki, _ := h["_wiki"].(bool); isWiki {
			s["type"] = "wiki"
			s["path"] = h["path"]
			s["title"] = h["title"]
			s["confidence"] = h["confidence"]
			s["status"] = h["status"]
		} else {
			s["type"] = "surrealdb"
			s["role"] = h["role"]
			if id := h["id"]; id != nil {
				s["id"] = fmt.Sprintf("%v", id)
			}
		}
		if dist, ok := h["dist"].(float64); ok {
			s["score"] = math.Round((1.0-dist)*10000) / 10000
		}
		sources = append(sources, s)
	}
	return sources
}

// ── Recall handler ────────────────────────────────────────────────────────────

func handleRecall(req recallRequest, emb *memory.Embedder, llm *memory.LLMClient) recallResponse {
	queryText := req.Query.UserMessage
	if req.Query.AssistantExcerpt != "" {
		queryText += " " + req.Query.AssistantExcerpt
	}

	var embedding []float64
	if emb != nil {
		if vec, err := emb.Embed(queryText); err == nil {
			embedding = vec
		}
	}

	scopes := entitledScopes(req.GroupID, req.Vignoble)
	seenEntities := make(map[string]bool)
	seenWiki := make(map[string]bool)

	var allHits []map[string]any
	for _, scope := range scopes {
		hits := queryScope(scope, queryText, embedding, seenEntities, seenWiki)
		allHits = append(allHits, hits...)
	}

	allHits = gateByRelevance(allHits)

	// Per-session dedup.
	if req.SessionID != "" {
		allHits = globalDedup.Filter(req.SessionID, allHits)
	}

	maxTokens := req.Constraints.MaxContextTokens
	if maxTokens <= 0 {
		maxTokens = 400
	}
	ctx := summarize(llm, allHits, maxTokens)
	if ctx != "" {
		ctx = "[memory] " + ctx
	}

	return recallResponse{
		Context: ctx,
		Sources: buildSources(allHits),
		Meta: map[string]any{
			"total_candidates": len(allHits),
		},
	}
}

// ── Boot-recall scope query ───────────────────────────────────────────────────

// bootHitsForScope queries one SurrealDB scope and returns compact manifest
// entries for the boot-recall response. Port of Python _boot_hits_for_scope.
//
// Content selection:
//   - vigne tier (scope == vigneScope): wiki + typed entities
//   - any other scope (vignoble-*, __global__): wiki only
func bootHitsForScope(scope, vigneScope, queryText string, embedding []float64, topK int) []bootEntry {
	db, err := surreal.New(scope)
	if err != nil {
		return nil
	}
	defer db.Close()
	if err := db.EnsureSchema(); err != nil {
		return nil
	}

	isVigne := scope == vigneScope

	// Wiki hits — always included.
	seenWiki := make(map[string]bool)
	var wikiHits []map[string]any
	if embedding != nil {
		if rows, err := db.RecallWiki(embedding, topK, false); err == nil {
			for _, r := range rows {
				path, _ := r["path"].(string)
				if !seenWiki[path] {
					seenWiki[path] = true
					wikiHits = append(wikiHits, r)
				}
			}
		}
	}
	// FTS fallback when embedding unavailable or to supplement.
	if rows, err := db.LookupWiki(queryText, topK, false); err == nil {
		for _, r := range rows {
			path, _ := r["path"].(string)
			if !seenWiki[path] {
				seenWiki[path] = true
				wikiHits = append(wikiHits, r)
			}
		}
	}

	// Typed entity hits — vigne tier only.
	var entityHits []map[string]any
	if isVigne {
		seenEntity := make(map[string]bool)
		var raw []map[string]any
		if embedding != nil {
			if rows, err := db.Recall(embedding, topK, ""); err == nil {
				raw = append(raw, rows...)
			}
		}
		if rows, err := db.Lookup(queryText, topK); err == nil {
			raw = append(raw, rows...)
		}
		for _, h := range raw {
			role, _ := h["role"].(string)
			if !typedEntityRoles[role] {
				continue
			}
			key := fmt.Sprintf("%s:%s", role, h["name"])
			if !seenEntity[key] {
				seenEntity[key] = true
				entityHits = append(entityHits, h)
			}
		}
	}

	// Shape into compact manifest entries.
	var result []bootEntry

	for _, h := range wikiHits {
		if len(result) >= topK {
			break
		}
		title, _ := h["title"].(string)
		if title == "" {
			continue
		}
		stored, _ := h["summary"].(string)
		body, _ := h["body"].(string)
		var summary string
		if stored != "" {
			summary = stored
		} else if body != "" {
			summary = body // client truncates
		} else {
			summary = title
		}
		path, _ := h["path"].(string)
		result = append(result, bootEntry{
			Scope:   scope,
			Type:    "wiki",
			Title:   title,
			Summary: summary,
			Ref:     "wiki:" + path,
		})
	}

	for _, h := range entityHits {
		if len(result) >= topK*2 { // wiki + entity cap
			break
		}
		rawName, _ := h["name"].(string)
		if rawName == "" {
			continue
		}
		title := cleanEntityTitle(rawName, 120)
		desc, _ := h["description"].(string)
		// Use full description; client does truncation (issue #228 fold-in).
		summary := stripEntityNoise(desc)
		// Null summary when it duplicates the title.
		summary = nullSummaryIfDuplicate(title, summary)
		// Build ref: entity ID already stringifies as "entity:<hash>".
		entityID := fmt.Sprintf("%v", h["id"])
		if !strings.HasPrefix(entityID, "entity:") {
			entityID = "entity:" + entityID
		}
		role, _ := h["role"].(string)
		result = append(result, bootEntry{
			Scope:   scope,
			Type:    role,
			Title:   title,
			Summary: summary,
			Ref:     entityID,
		})
	}

	return result
}

// ── Main recall loop ──────────────────────────────────────────────────────────

func runRecall(vignoble string) error {
	natsURL := envOr("NATS_URL", "nats://localhost:4222")
	nc, err := nats.Connect(natsURL,
		nats.Name("memory-recall"),
		nats.ReconnectWait(5*time.Second),
		nats.MaxReconnects(-1),
	)
	if err != nil {
		return fmt.Errorf("NATS connect: %w", err)
	}
	defer nc.Close()
	log.Printf("INFO: memory-recall connected vignoble=%s", vignoble)

	emb := memory.NewEmbedder()
	llm, llmErr := memory.BuildLLMClient()
	if llmErr != nil {
		log.Printf("WARN: LLM client unavailable: %v — will degrade to verbatim recall", llmErr)
		llm = nil
	}

	// Main recall subscription — wildcard serves all vignobles from one process.
	_, err = nc.Subscribe("pinard.*.recall", func(msg *nats.Msg) {
		var req recallRequest
		if err := json.Unmarshal(msg.Data, &req); err != nil {
			_ = msg.Respond(nullResponse())
			return
		}
		resp := handleRecall(req, emb, llm)
		b, _ := json.Marshal(resp)
		_ = msg.Respond(b)
	})
	if err != nil {
		return fmt.Errorf("subscribe recall: %w", err)
	}

	// Boot recall subscription (hierarchical scope injection at spawn) — wildcard
	// serves all vignobles. Returns v2 bootRecallResponse{entries:[...]} matching
	// bootRecallResponse in cmd/aoc/cmd_memory_boot.go.
	_, err = nc.Subscribe("pinard.*.recall.boot", func(msg *nats.Msg) {
		var req bootRecallRequest
		if err := json.Unmarshal(msg.Data, &req); err != nil {
			_ = msg.Respond(nullBootResponse())
			return
		}
		if len(req.Scopes) == 0 {
			_ = msg.Respond(nullBootResponse())
			return
		}
		topK := req.TopK
		if topK <= 0 {
			topK = 5
		}
		queryText := req.TaskText
		if queryText == "" {
			queryText = "accumulated operational knowledge"
		}
		// Pre-compute embedding once for all scopes.
		var embedding []float64
		if vec, err := emb.Embed(queryText); err == nil {
			embedding = vec
		}
		// Fan out across scopes; vigne scope is the agent's own group_id.
		var allEntries []bootEntry
		for _, scope := range req.Scopes {
			entries := bootHitsForScope(scope, req.GroupID, queryText, embedding, topK)
			allEntries = append(allEntries, entries...)
		}
		var resp bootRecallResponse
		if allEntries != nil {
			resp.Entries = allEntries
		} else {
			resp.Entries = []bootEntry{}
		}
		resp.Meta.TotalEntries = len(resp.Entries)
		b, _ := json.Marshal(resp)
		_ = msg.Respond(b)
	})
	if err != nil {
		log.Printf("WARN: subscribe boot-recall: %v", err)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	<-ctx.Done()
	log.Printf("INFO: memory-recall shutting down")
	return nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
