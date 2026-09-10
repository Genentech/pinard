package memory

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	// postgres driver — blank import
	_ "github.com/lib/pq"
)

// EngramPostgresReaderError is returned on Postgres errors.
type EngramPostgresReaderError struct {
	Message string
}

func (e *EngramPostgresReaderError) Error() string { return "engram_pg: " + e.Message }

// EngramPostgresReader reads curated observations from the Engram cloud RDS.
//
// Environment variables:
//
//	ENGRAM_PG_DSN      — Postgres DSN (required)
//	MEMORY_PG_BATCH    — rows per poll (default: 1000)
type EngramPostgresReader struct {
	Project   string
	DSN       string
	BatchSize int
}

// NewEngramPostgresReader creates a reader using environment defaults.
func NewEngramPostgresReader(project string) *EngramPostgresReader {
	batch := 1000
	if s := os.Getenv("MEMORY_PG_BATCH"); s != "" {
		if n, err := strconv.Atoi(s); err == nil {
			batch = n
		}
	}
	return &EngramPostgresReader{
		Project:   project,
		DSN:       os.Getenv("ENGRAM_PG_DSN"),
		BatchSize: batch,
	}
}

// pgMutation is a row from the cloud_mutations table.
type pgMutation struct {
	Seq        int64
	Project    string
	Entity     string
	EntityKey  string
	Op         string
	Payload    json.RawMessage
	OccurredAt time.Time
}

// DiscoverProjects returns the distinct project names that have observation
// upserts recorded in cloud_mutations. This is used by the ingester to
// auto-discover all groups without a hard-coded list.
func (r *EngramPostgresReader) DiscoverProjects() ([]string, error) {
	if r.DSN == "" {
		return nil, &EngramPostgresReaderError{Message: "ENGRAM_PG_DSN not set"}
	}

	db, err := sql.Open("postgres", r.DSN)
	if err != nil {
		return nil, &EngramPostgresReaderError{Message: fmt.Sprintf("open: %v", err)}
	}
	defer db.Close()

	rows, err := db.Query(
		`SELECT DISTINCT project FROM cloud_mutations WHERE entity='observation' AND op='upsert' ORDER BY project`,
	)
	if err != nil {
		return nil, &EngramPostgresReaderError{Message: fmt.Sprintf("discover: %v", err)}
	}
	defer rows.Close()

	var projects []string
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			continue
		}
		projects = append(projects, p)
	}
	return projects, rows.Err()
}

// Fetch returns observations since lastSeq. It also returns the new max seq.
func (r *EngramPostgresReader) Fetch(lastSeq int64) ([]EngramObservation, int64, error) {
	if r.DSN == "" {
		return nil, lastSeq, &EngramPostgresReaderError{Message: "ENGRAM_PG_DSN not set"}
	}

	db, err := sql.Open("postgres", r.DSN)
	if err != nil {
		return nil, lastSeq, &EngramPostgresReaderError{Message: fmt.Sprintf("open: %v", err)}
	}
	defer db.Close()

	rows, err := db.Query(
		`SELECT seq, project, entity, entity_key, op, payload, occurred_at
		 FROM cloud_mutations
		 WHERE project=$1 AND seq>$2 AND entity='observation' AND op='upsert'
		 ORDER BY seq ASC LIMIT $3`,
		r.Project, lastSeq, r.BatchSize,
	)
	if err != nil {
		return nil, lastSeq, &EngramPostgresReaderError{Message: fmt.Sprintf("query: %v", err)}
	}
	defer rows.Close()

	var out []EngramObservation
	maxSeq := lastSeq
	for rows.Next() {
		var m pgMutation
		if err := rows.Scan(&m.Seq, &m.Project, &m.Entity, &m.EntityKey, &m.Op, &m.Payload, &m.OccurredAt); err != nil {
			continue
		}
		if m.Seq > maxSeq {
			maxSeq = m.Seq
		}
		if m.Op != "upsert" {
			continue
		}
		var payload map[string]any
		if err := json.Unmarshal(m.Payload, &payload); err != nil {
			continue
		}
		obs := parseEngramItem(payload, r.Project)
		out = append(out, obs)
	}
	return out, maxSeq, rows.Err()
}

// MaxSeq returns the maximum seq in cloud_mutations for this project, or 0.
func (r *EngramPostgresReader) MaxSeq() (int64, error) {
	if r.DSN == "" {
		return 0, &EngramPostgresReaderError{Message: "ENGRAM_PG_DSN not set"}
	}
	db, err := sql.Open("postgres", r.DSN)
	if err != nil {
		return 0, &EngramPostgresReaderError{Message: fmt.Sprintf("open: %v", err)}
	}
	defer db.Close()

	var max int64
	err = db.QueryRow(
		`SELECT COALESCE(MAX(seq), 0) FROM cloud_mutations WHERE project=$1 AND entity='observation'`,
		r.Project,
	).Scan(&max)
	if err != nil {
		return 0, &EngramPostgresReaderError{Message: fmt.Sprintf("max seq: %v", err)}
	}
	return max, nil
}
