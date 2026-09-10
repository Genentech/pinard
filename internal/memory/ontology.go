package memory

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/surreal"
)

// GardenerConfig holds configuration for the ontology gardener.
type GardenerConfig struct {
	// MinOccurrence is the minimum occurrence_count to consider an entity/edge
	// staging row for gardener analysis (default: 2).
	MinOccurrence int
	// Limit is the maximum number of staging rows to fetch per run (default: 200).
	Limit int
	// RepoDir is the wiki repo root where ontology-proposals/ YAML files are written.
	RepoDir string
}

// DefaultGardenerConfig returns sensible defaults.
func DefaultGardenerConfig() GardenerConfig {
	repoDir := envOr("WIKI_ROOT", envOr("VIGNOBLE_DIR", "."))
	return GardenerConfig{
		MinOccurrence: 2,
		Limit:         200,
		RepoDir:       repoDir,
	}
}

// GardenerDecision is the outcome for a staging cluster.
type GardenerDecision string

const (
	GardenerMap    GardenerDecision = "Map"
	GardenerExtend GardenerDecision = "Extend"
	GardenerHold   GardenerDecision = "Hold"
)

// GardenerResult records the gardener's decision for one cluster.
type GardenerResult struct {
	Decision    GardenerDecision
	ClusterName string
	Rationale   string
	ProposalID  string // set for Extend decisions
}

// RunGardener runs the ontology gardener for a given group_id.
// It mines entity_staging / edge_staging, clusters proposals, and emits
// YAML proposal files for Extend decisions.
func RunGardener(groupID string, db *surreal.Client, llm *LLMClient, cfg GardenerConfig) ([]GardenerResult, error) {
	entityRows, err := db.ListEntityStaging(cfg.MinOccurrence, cfg.Limit)
	if err != nil {
		return nil, fmt.Errorf("list entity_staging: %w", err)
	}
	edgeRows, err := db.ListEdgeStaging(cfg.MinOccurrence, cfg.Limit)
	if err != nil {
		return nil, fmt.Errorf("list edge_staging: %w", err)
	}

	if len(entityRows)+len(edgeRows) == 0 {
		return nil, nil
	}

	// Cluster entity staging rows by embedding similarity.
	entityClusters := clusterStagingRows(entityRows)
	edgeClusters := clusterStagingRows(edgeRows)

	var results []GardenerResult

	// Make LLM-driven decisions for each cluster.
	for _, cl := range entityClusters {
		if llm == nil {
			results = append(results, GardenerResult{
				Decision:    GardenerHold,
				ClusterName: clusterRepName(cl),
				Rationale:   "LLM unavailable",
			})
			continue
		}
		decision, rationale, err := decideStagingCluster(llm, cl, "entity")
		if err != nil {
			log.Printf("WARN: gardener decide cluster %q: %v", clusterRepName(cl), err)
			continue
		}
		r := GardenerResult{
			Decision:    decision,
			ClusterName: clusterRepName(cl),
			Rationale:   rationale,
		}
		if decision == GardenerExtend {
			id, err := emitProposalMR(cl, "entity", rationale, cfg.RepoDir)
			if err != nil {
				log.Printf("WARN: emit proposal MR: %v", err)
			}
			r.ProposalID = id
		}
		results = append(results, r)
	}

	for _, cl := range edgeClusters {
		if llm == nil {
			results = append(results, GardenerResult{
				Decision:    GardenerHold,
				ClusterName: clusterRepName(cl),
				Rationale:   "LLM unavailable",
			})
			continue
		}
		decision, rationale, err := decideStagingCluster(llm, cl, "edge")
		if err != nil {
			log.Printf("WARN: gardener decide cluster %q: %v", clusterRepName(cl), err)
			continue
		}
		r := GardenerResult{
			Decision:    decision,
			ClusterName: clusterRepName(cl),
			Rationale:   rationale,
		}
		if decision == GardenerExtend {
			id, err := emitProposalMR(cl, "edge", rationale, cfg.RepoDir)
			if err != nil {
				log.Printf("WARN: emit proposal MR: %v", err)
			}
			r.ProposalID = id
		}
		results = append(results, r)
	}

	return results, nil
}

// ── Internal helpers ──────────────────────────────────────────────────────────

type stagingCluster []map[string]any

func clusterStagingRows(rows []map[string]any) []stagingCluster {
	var clusters []stagingCluster
	for _, row := range rows {
		embRaw := row["embedding"]
		vec := toFloat64Slice(embRaw)
		placed := false
		for i, cl := range clusters {
			for _, ce := range cl {
				ceVec := toFloat64Slice(ce["embedding"])
				if cosineSimilarity(vec, ceVec) >= 0.80 {
					clusters[i] = append(clusters[i], row)
					placed = true
					break
				}
			}
			if placed {
				break
			}
		}
		if !placed {
			clusters = append(clusters, stagingCluster{row})
		}
	}
	return clusters
}

func clusterRepName(cl stagingCluster) string {
	if len(cl) == 0 {
		return ""
	}
	if name, ok := cl[0]["name"].(string); ok {
		return name
	}
	if name, ok := cl[0]["from_name"].(string); ok {
		return name
	}
	return ""
}

func decideStagingCluster(llm *LLMClient, cl stagingCluster, kind string) (GardenerDecision, string, error) {
	names := make([]string, 0, len(cl))
	for _, row := range cl {
		if kind == "entity" {
			if n, ok := row["name"].(string); ok {
				names = append(names, n)
			}
		} else {
			from, _ := row["from_name"].(string)
			rel, _ := row["proposed_relation"].(string)
			to, _ := row["to_name"].(string)
			names = append(names, fmt.Sprintf("%s -[%s]-> %s", from, rel, to))
		}
	}

	prompt := fmt.Sprintf(
		"You are the pinard ontology gardener. The following %s proposals appear repeatedly in the staging table. "+
			"Decide: Map (fits existing ontology), Extend (genuinely new concept worth adding), or Hold (noise/unclear).\n\n"+
			"Proposals:\n%s\n\n"+
			"Reply with exactly: Decision: <Map|Extend|Hold>\nRationale: <one sentence>",
		kind, strings.Join(names, "\n"),
	)
	resp, err := llm.Complete([]LLMMessage{{Role: "user", Content: prompt}}, 256, "")
	if err != nil {
		return GardenerHold, "", err
	}

	decision := GardenerHold
	rationale := resp
	for _, line := range strings.Split(resp, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "Decision:") {
			d := strings.TrimSpace(strings.TrimPrefix(line, "Decision:"))
			switch strings.ToLower(d) {
			case "map":
				decision = GardenerMap
			case "extend":
				decision = GardenerExtend
			case "hold":
				decision = GardenerHold
			}
		} else if strings.HasPrefix(line, "Rationale:") {
			rationale = strings.TrimSpace(strings.TrimPrefix(line, "Rationale:"))
		}
	}
	return decision, rationale, nil
}

// emitProposalMR writes a YAML proposal file to repoDir/ontology-proposals/<id>.yaml.
// The human reviews it and creates a corresponding change in the pinard repo.
func emitProposalMR(cl stagingCluster, kind, rationale, repoDir string) (string, error) {
	proposalID := fmt.Sprintf("%s-%d", kind, time.Now().UnixMilli())
	proposalDir := filepath.Join(repoDir, "ontology-proposals")
	if err := os.MkdirAll(proposalDir, 0755); err != nil {
		return "", fmt.Errorf("mkdir %s: %w", proposalDir, err)
	}

	names := make([]string, 0, len(cl))
	for _, row := range cl {
		if kind == "entity" {
			if n, ok := row["name"].(string); ok {
				names = append(names, n)
			}
		} else {
			from, _ := row["from_name"].(string)
			rel, _ := row["proposed_relation"].(string)
			to, _ := row["to_name"].(string)
			names = append(names, fmt.Sprintf("%s -[%s]-> %s", from, rel, to))
		}
	}

	proposal := map[string]any{
		"id":         proposalID,
		"kind":       kind,
		"names":      names,
		"rationale":  rationale,
		"created_at": time.Now().UTC().Format(time.RFC3339),
	}
	data, err := json.MarshalIndent(proposal, "", "  ")
	if err != nil {
		return "", err
	}
	path := filepath.Join(proposalDir, proposalID+".json")
	if err := os.WriteFile(path, data, 0644); err != nil {
		return "", fmt.Errorf("write proposal: %w", err)
	}
	log.Printf("INFO: ontology proposal written: %s", path)
	return proposalID, nil
}

// cosineSimilarity and toFloat64Slice are shared with memory package.
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
