// Package engram provides helpers for querying the local engram memory store.
package engram

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"time"

	_ "modernc.org/sqlite"
)

// DBStatus holds the sync state of a vignoble's local engram store.
type DBStatus struct {
	// DBExists is false when the database file is absent (no engram store yet).
	DBExists bool
	// Total is the count of non-deleted observations.
	Total int
	// Pending is the count of observations not yet pushed to the cloud (sync_id IS NULL).
	// Deprecated: use UnackedMutations for accurate cloud-push status.
	Pending int

	// SyncStatePresent is true when the sync_state table exists in the db.
	SyncStatePresent bool
	// SyncLifecycle is the lifecycle field of the cloud target row in sync_state
	// (e.g. "active", "degraded"). Empty when sync_state is absent or has no cloud row.
	SyncLifecycle string
	// UnackedMutations is the count of rows in sync_mutations with acked_at IS NULL.
	// This is the authoritative "pending push" count.
	UnackedMutations int
	// ReasonCode is the reason_code from the cloud sync_state row (e.g. "non_enrolled_pending_mutations").
	ReasonCode string
	// LastError is the last_error from the cloud sync_state row.
	LastError string
	// LastSync is the updated_at timestamp of the cloud sync_state row. It reflects
	// the most recent cloud-ack activity regardless of which path (daemon drain or
	// conductor autosync) performed the sync. Nil when sync_state is absent, has no
	// cloud row, or the updated_at value is missing/unparseable.
	LastSync *time.Time
}

// IsDegraded reports whether the cloud sync target is in a degraded state.
func (s *DBStatus) IsDegraded() bool {
	return s.SyncStatePresent && s.SyncLifecycle == "degraded"
}

// ReasonPhrase returns a short, human-readable phrase for the cloud sync
// reason_code (falling back to the raw code, then last_error). Empty when none.
func (s *DBStatus) ReasonPhrase() string {
	switch s.ReasonCode {
	case "":
		return s.LastError
	case "non_enrolled_pending_mutations":
		return "not enrolled"
	default:
		return s.ReasonCode
	}
}

// SyncRecord is the freshness record written by the daemon's EngramSyncer to
// NATS KV (bucket "pinard-engram", key = vignoble name).
type SyncRecord struct {
	LastSync   time.Time `json:"last_sync,omitempty"`
	Result     string    `json:"result"` // "ok" | "error" | "skipped"
	Error      string    `json:"error,omitempty"`
	Pending    int       `json:"pending,omitempty"`
	Degraded   bool      `json:"degraded,omitempty"`
	ReasonCode string    `json:"reason_code,omitempty"`
}

// QueryStatus opens the engram SQLite database at dbPath (read-only) and
// returns the total and pending observation counts, plus sync_state/sync_mutations
// data for accurate cloud-push status. If the file does not exist, DBExists is
// false and the counts are zero. Never returns an error for a missing file —
// only for unexpected failures. Gracefully handles missing sync_state/sync_mutations
// tables (older engram stores).
func QueryStatus(dbPath string) (*DBStatus, error) {
	if _, err := os.Stat(dbPath); errors.Is(err, os.ErrNotExist) {
		return &DBStatus{DBExists: false}, nil
	}

	// Open read-only (URI mode).
	dsn := fmt.Sprintf("file:%s?mode=ro&_journal_mode=WAL&cache=shared", dbPath)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open engram db: %w", err)
	}
	defer db.Close()

	var total, pending int
	row := db.QueryRow(
		`SELECT count(*), count(CASE WHEN sync_id IS NULL THEN 1 END)
		 FROM observations WHERE deleted_at IS NULL`,
	)
	if err := row.Scan(&total, &pending); err != nil {
		return nil, fmt.Errorf("query engram db: %w", err)
	}

	st := &DBStatus{
		DBExists: true,
		Total:    total,
		Pending:  pending,
	}

	// Query sync_state for the cloud target. Wrap in a table-existence check
	// so we degrade gracefully on older engram stores that lack the table.
	var syncStateExists int
	_ = db.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sync_state'`,
	).Scan(&syncStateExists)

	if syncStateExists > 0 {
		st.SyncStatePresent = true
		var lifecycle, reasonCode, lastError, updatedAt sql.NullString
		// NOTE: the sync_state primary key column is `target_key` (not `target`).
		err := db.QueryRow(
			`SELECT lifecycle, reason_code, last_error, updated_at FROM sync_state WHERE target_key='cloud' LIMIT 1`,
		).Scan(&lifecycle, &reasonCode, &lastError, &updatedAt)
		if err == nil {
			st.SyncLifecycle = lifecycle.String
			st.ReasonCode = reasonCode.String
			st.LastError = lastError.String
			if updatedAt.Valid && updatedAt.String != "" {
				if t, err := time.Parse("2006-01-02 15:04:05", updatedAt.String); err == nil {
					utc := t.UTC()
					st.LastSync = &utc
				} else if t, err := time.Parse(time.RFC3339, updatedAt.String); err == nil {
					utc := t.UTC()
					st.LastSync = &utc
				}
			}
		}
		// err == sql.ErrNoRows means no cloud row yet — leave fields empty.
	}

	// Count unacked mutations for authoritative pending-push count.
	var syncMutationsExists int
	_ = db.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sync_mutations'`,
	).Scan(&syncMutationsExists)

	if syncMutationsExists > 0 {
		var unacked int
		if err := db.QueryRow(
			`SELECT count(*) FROM sync_mutations WHERE acked_at IS NULL`,
		).Scan(&unacked); err == nil {
			st.UnackedMutations = unacked
		}
	}

	return st, nil
}
