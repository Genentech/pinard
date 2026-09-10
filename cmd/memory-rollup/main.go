// Command memory-rollup is the Go port of services/memory/rollup.py.
//
// Reads vignes.yaml to discover group_ids, then aggregates cross-vigne
// knowledge into vignoble-level and global-level namespaces.
//
// Environment variables:
//
//	VIGNOBLE_YAML       — path to vignes.yaml
//	VIGNOBLE_DIR        — vignoble root (used to find vignes.yaml)
//	VIGNOBLES_BASE_DIR  — parent dir of multiple vignoble clones
//	SURREAL_URL         — SurrealDB endpoint
//	SURREAL_USER        — SurrealDB username
//	SURREAL_PASS        — SurrealDB password
//	ROSETTA_URL         — Rosetta embedding endpoint
//	MEMORY_LLM_*        — LLM client config
//	ROLLUP_THRESHOLD    — min vignes per cluster to promote (default: 2)
//	ROLLUP_INTERVAL     — run interval (e.g. 1h, 30m); unset/0 = one-shot
package main

import (
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/memory"
	"github.com/Genentech/pinard/internal/surreal"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

func main() {
	root := &cobra.Command{
		Use:   "memory-rollup",
		Short: "Pinard scope roll-up engine",
		RunE:  run,
	}
	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

func run(cmd *cobra.Command, args []string) error {
	emb := memory.NewEmbedder()
	llm, llmErr := memory.BuildLLMClient()
	if llmErr != nil {
		log.Printf("WARN: LLM client unavailable: %v — wiki synthesis disabled", llmErr)
		llm = nil
	}

	threshold := 2
	if s := os.Getenv("ROLLUP_THRESHOLD"); s != "" {
		if _, err := fmt.Sscanf(s, "%d", &threshold); err != nil {
			threshold = 2
		}
	}

	var interval time.Duration
	if s := os.Getenv("ROLLUP_INTERVAL"); s != "" {
		if d, err := time.ParseDuration(s); err == nil && d > 0 {
			interval = d
		}
	}

	for {
		memberships, err := loadAllVignobleMemberships()
		if err != nil {
			log.Printf("ERROR: load memberships: %v", err)
		} else {
			if err := runRollup(memberships, threshold, emb, llm); err != nil {
				log.Printf("ERROR: rollup: %v", err)
			}
		}
		if interval == 0 {
			return nil
		}
		log.Printf("INFO: next rollup in %s", interval)
		time.Sleep(interval)
	}
}

// ── Vignoble membership discovery ────────────────────────────────────────────

type vignesYAML struct {
	Vignes map[string]struct {
		Repo string `yaml:"repo"`
	} `yaml:"vignes"`
}

func loadVignesYAML(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg vignesYAML
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	var ids []string
	for name := range cfg.Vignes {
		ids = append(ids, name)
	}
	return ids, nil
}

// loadAllVignobleMemberships returns a map of vignoble-name → []group_id.
func loadAllVignobleMemberships() (map[string][]string, error) {
	result := make(map[string][]string)

	// If VIGNOBLES_BASE_DIR is set, scan all subdirectories.
	if baseDir := os.Getenv("VIGNOBLES_BASE_DIR"); baseDir != "" {
		entries, err := os.ReadDir(baseDir)
		if err != nil {
			return nil, fmt.Errorf("read VIGNOBLES_BASE_DIR %s: %w", baseDir, err)
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			vigPath := filepath.Join(baseDir, e.Name(), "vignes.yaml")
			if _, err := os.Stat(vigPath); os.IsNotExist(err) {
				continue
			}
			ids, err := loadVignesYAML(vigPath)
			if err != nil {
				log.Printf("WARN: load %s: %v", vigPath, err)
				continue
			}
			vignobleName := e.Name()
			vignobleName = strings.TrimPrefix(vignobleName, "vignoble-")
			result[vignobleName] = ids
		}
		return result, nil
	}

	// Single vignoble from VIGNOBLE_YAML or VIGNOBLE_DIR.
	vigPath := os.Getenv("VIGNOBLE_YAML")
	if vigPath == "" {
		vigDir := envOr("VIGNOBLE_DIR", ".")
		vigPath = filepath.Join(vigDir, "vignes.yaml")
	}
	ids, err := loadVignesYAML(vigPath)
	if err != nil {
		return nil, fmt.Errorf("load %s: %w", vigPath, err)
	}
	// Derive vignoble name from the directory.
	vignobleName := filepath.Base(filepath.Dir(vigPath))
	vignobleName = strings.TrimPrefix(vignobleName, "vignoble-")
	result[vignobleName] = ids
	return result, nil
}

// ── Clustering ────────────────────────────────────────────────────────────────

type entityCluster struct {
	Entities    []map[string]any
	ViGnobleSet map[string]bool
}

// cosineSimilarity computes cosine similarity between two vectors.
func cosineSimilarity(a, b []float64) float64 {
	if len(a) != len(b) || len(a) == 0 {
		return 0
	}
	var dot, normA, normB float64
	for i := range a {
		dot += a[i] * b[i]
		normA += a[i] * a[i]
		normB += b[i] * b[i]
	}
	if normA == 0 || normB == 0 {
		return 0
	}
	return dot / (math.Sqrt(normA) * math.Sqrt(normB))
}

const clusterSimilarityThreshold = 0.85

// clusterEntities groups entities by embedding similarity.
func clusterEntities(entities []map[string]any) []entityCluster {
	var clusters []entityCluster

	for _, e := range entities {
		embRaw, ok := e["embedding"]
		if !ok {
			continue
		}
		vec := toFloat64Slice(embRaw)
		vignobleName, _ := e["_vignoble"].(string)

		bestCluster := -1
		bestSim := 0.0
		for i, cl := range clusters {
			for _, ce := range cl.Entities {
				ceVec := toFloat64Slice(ce["embedding"])
				sim := cosineSimilarity(vec, ceVec)
				if sim > bestSim {
					bestSim = sim
					bestCluster = i
				}
			}
		}
		if bestCluster >= 0 && bestSim >= clusterSimilarityThreshold {
			clusters[bestCluster].Entities = append(clusters[bestCluster].Entities, e)
			if vignobleName != "" {
				clusters[bestCluster].ViGnobleSet[vignobleName] = true
			}
		} else {
			cl := entityCluster{
				Entities:    []map[string]any{e},
				ViGnobleSet: make(map[string]bool),
			}
			if vignobleName != "" {
				cl.ViGnobleSet[vignobleName] = true
			}
			clusters = append(clusters, cl)
		}
	}
	return clusters
}

func toFloat64Slice(v any) []float64 {
	switch t := v.(type) {
	case []float64:
		return t
	case []any:
		out := make([]float64, len(t))
		for i, f := range t {
			if fv, ok := f.(float64); ok {
				out[i] = fv
			}
		}
		return out
	}
	return nil
}

// ── Roll-up engine ────────────────────────────────────────────────────────────

func runRollup(memberships map[string][]string, threshold int, emb *memory.Embedder, llm *memory.LLMClient) error {
	// For each vignoble: collect entities from all member vignes, cluster,
	// promote cross-vigne clusters to vignoble scope.
	for vignobleName, groupIDs := range memberships {
		vignobleDB := "vignoble-" + vignobleName
		if err := promoteToScope(groupIDs, vignobleDB, vignobleName, threshold, emb, llm); err != nil {
			log.Printf("ERROR: rollup for vignoble %s: %v", vignobleName, err)
		}
	}

	// Global rollup: promote cross-vignoble clusters to __global__.
	var allVignobleDBs []string
	for vignobleName := range memberships {
		allVignobleDBs = append(allVignobleDBs, "vignoble-"+vignobleName)
	}
	if len(allVignobleDBs) >= threshold {
		if err := promoteToScope(allVignobleDBs, "__global__", "global", threshold, emb, llm); err != nil {
			log.Printf("ERROR: global rollup: %v", err)
		}
	}
	return nil
}

func promoteToScope(srcGroupIDs []string, dstGroupID, scopeLabel string, threshold int, emb *memory.Embedder, llm *memory.LLMClient) error {
	// Collect wiki_doc pages from all source scopes.
	var allEntities []map[string]any
	for _, gid := range srcGroupIDs {
		db, err := surreal.New(gid)
		if err != nil {
			log.Printf("WARN: connect %s: %v", gid, err)
			continue
		}
		rows, err := db.Query("SELECT title, body, summary, embedding FROM wiki_doc WHERE status='auto_serve'", nil)
		db.Close()
		if err != nil {
			log.Printf("WARN: fetch wiki_doc %s: %v", gid, err)
			continue
		}
		for _, rowSet := range rows {
			if pages, ok := rowSet.([]any); ok {
				for _, p := range pages {
					if page, ok := p.(map[string]any); ok {
						page["_source_gid"] = gid
						page["_vignoble"] = scopeLabel
						allEntities = append(allEntities, page)
					}
				}
			}
		}
	}

	if len(allEntities) == 0 {
		return nil
	}

	// Cluster by embedding similarity.
	clusters := clusterEntities(allEntities)

	// Open destination DB.
	dstDB, err := surreal.New(dstGroupID)
	if err != nil {
		return fmt.Errorf("connect dst %s: %w", dstGroupID, err)
	}
	defer dstDB.Close()
	if err := dstDB.EnsureSchema(); err != nil {
		return fmt.Errorf("schema dst %s: %w", dstGroupID, err)
	}

	synthesized := 0
	for _, cl := range clusters {
		if len(cl.ViGnobleSet) < threshold {
			continue
		}
		if err := synthesizeAndUpsert(cl, dstDB, dstGroupID, emb, llm); err != nil {
			log.Printf("WARN: synthesize cluster: %v", err)
		} else {
			synthesized++
		}
	}

	log.Printf("INFO: rollup scope=%s clusters=%d synthesized=%d", dstGroupID, len(clusters), synthesized)
	return nil
}

func synthesizeAndUpsert(cl entityCluster, dstDB *surreal.Client, scope string, emb *memory.Embedder, llm *memory.LLMClient) error {
	// Build content from cluster members.
	var parts []string
	for _, e := range cl.Entities {
		title, _ := e["title"].(string)
		body, _ := e["body"].(string)
		if len(body) > 600 {
			body = body[:600]
		}
		parts = append(parts, fmt.Sprintf("## %s\n%s", title, body))
	}
	combined := strings.Join(parts, "\n\n")

	path := fmt.Sprintf("rollup/%s", sanitizePathSegment(cl.Entities[0]["title"]))
	title := fmt.Sprintf("Consolidated: %v", cl.Entities[0]["title"])

	var body string
	if llm != nil {
		synthesized, err := llm.Complete([]memory.LLMMessage{
			{Role: "user", Content: "Consolidate the following knowledge items into a single cohesive wiki page:\n\n" + combined},
		}, 1024, "You are a technical knowledge consolidator. Output clean markdown.")
		if err != nil {
			log.Printf("WARN: LLM synthesis: %v", err)
			body = combined
		} else {
			body = synthesized
		}
	} else {
		body = combined
	}

	var vec []float64
	if emb != nil {
		if v, err := emb.Embed(title + "\n" + body[:min(500, len(body))]); err == nil {
			vec = v
		}
	}

	_, err := dstDB.UpsertWikiDoc(title, body, path, "", 0.9, map[string]any{"source": "rollup", "scope": scope}, vec)
	return err
}

func sanitizePathSegment(v any) string {
	s := fmt.Sprintf("%v", v)
	s = strings.ToLower(s)
	var b strings.Builder
	for _, ch := range s {
		if (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '-' {
			b.WriteRune(ch)
		} else {
			b.WriteRune('-')
		}
	}
	return strings.Trim(b.String(), "-")
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
