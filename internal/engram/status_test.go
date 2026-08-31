package engram

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

func createTestDB(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "engram.db")

	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("create test db: %v", err)
	}
	defer db.Close()

	_, err = db.Exec(`CREATE TABLE sessions (
		id TEXT PRIMARY KEY,
		created_at TEXT NOT NULL DEFAULT (datetime('now'))
	)`)
	if err != nil {
		t.Fatalf("create sessions table: %v", err)
	}

	_, err = db.Exec(`CREATE TABLE observations (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		sync_id    TEXT,
		session_id TEXT NOT NULL,
		type       TEXT NOT NULL,
		title      TEXT NOT NULL,
		content    TEXT NOT NULL,
		deleted_at TEXT
	)`)
	if err != nil {
		t.Fatalf("create observations table: %v", err)
	}

	_, err = db.Exec(`INSERT INTO sessions (id) VALUES ('sess1')`)
	if err != nil {
		t.Fatalf("insert session: %v", err)
	}

	return dbPath
}

func insertObs(t *testing.T, dbPath string, syncID *string) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()

	_, err = db.Exec(
		`INSERT INTO observations (sync_id, session_id, type, title, content) VALUES (?, 'sess1', 'note', 'title', 'content')`,
		syncID,
	)
	if err != nil {
		t.Fatalf("insert observation: %v", err)
	}
}

func TestQueryStatus_MissingDB(t *testing.T) {
	status, err := QueryStatus("/nonexistent/path/engram.db")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if status.DBExists {
		t.Error("expected DBExists=false for missing file")
	}
	if status.Total != 0 || status.Pending != 0 {
		t.Errorf("expected 0/0, got %d/%d", status.Total, status.Pending)
	}
}

func TestQueryStatus_EmptyDB(t *testing.T) {
	dbPath := createTestDB(t)
	status, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !status.DBExists {
		t.Error("expected DBExists=true")
	}
	if status.Total != 0 || status.Pending != 0 {
		t.Errorf("expected 0/0, got %d/%d", status.Total, status.Pending)
	}
}

func TestQueryStatus_AllSynced(t *testing.T) {
	dbPath := createTestDB(t)
	syncID := "cloud-id-1"
	insertObs(t, dbPath, &syncID)
	insertObs(t, dbPath, &syncID)

	status, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !status.DBExists {
		t.Error("expected DBExists=true")
	}
	if status.Total != 2 {
		t.Errorf("expected Total=2, got %d", status.Total)
	}
	if status.Pending != 0 {
		t.Errorf("expected Pending=0, got %d", status.Pending)
	}
}

func TestQueryStatus_WithPending(t *testing.T) {
	dbPath := createTestDB(t)
	syncID := "cloud-id-1"
	insertObs(t, dbPath, &syncID) // synced
	insertObs(t, dbPath, nil)     // pending
	insertObs(t, dbPath, nil)     // pending

	status, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if status.Total != 3 {
		t.Errorf("expected Total=3, got %d", status.Total)
	}
	if status.Pending != 2 {
		t.Errorf("expected Pending=2, got %d", status.Pending)
	}
}

func TestQueryStatus_DeletedExcluded(t *testing.T) {
	dbPath := createTestDB(t)
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	_, _ = db.Exec(`INSERT INTO sessions (id) VALUES ('sess1')`)
	// synced but deleted — should not count
	_, err = db.Exec(
		`INSERT INTO observations (sync_id, session_id, type, title, content, deleted_at) VALUES ('sid', 'sess1', 'note', 't', 'c', '2024-01-01')`,
	)
	if err != nil {
		t.Fatalf("insert: %v", err)
	}
	// pending and not deleted
	_, _ = db.Exec(
		`INSERT INTO observations (sync_id, session_id, type, title, content) VALUES (NULL, 'sess1', 'note', 't', 'c')`,
	)
	db.Close()

	status, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if status.Total != 1 {
		t.Errorf("expected Total=1 (deleted excluded), got %d", status.Total)
	}
	if status.Pending != 1 {
		t.Errorf("expected Pending=1, got %d", status.Pending)
	}
}

func TestQueryStatus_TempDirRemoved(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "engram.db")
	// Create and immediately remove to simulate truly missing file
	f, _ := os.Create(dbPath)
	f.Close()
	os.Remove(dbPath)

	status, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if status.DBExists {
		t.Error("expected DBExists=false")
	}
}

// createSyncTables adds sync_state and sync_mutations tables to an existing test db.
func createSyncTables(t *testing.T, dbPath string) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()

	_, err = db.Exec(`CREATE TABLE sync_state (
		target_key        TEXT PRIMARY KEY,
		lifecycle         TEXT,
		last_enqueued_seq INTEGER DEFAULT 0,
		last_acked_seq    INTEGER DEFAULT 0,
		reason_code       TEXT,
		last_error        TEXT,
		updated_at        TEXT DEFAULT (datetime('now'))
	)`)
	if err != nil {
		t.Fatalf("create sync_state: %v", err)
	}

	_, err = db.Exec(`CREATE TABLE sync_mutations (
		id       INTEGER PRIMARY KEY AUTOINCREMENT,
		acked_at TEXT
	)`)
	if err != nil {
		t.Fatalf("create sync_mutations: %v", err)
	}
}

func insertSyncState(t *testing.T, dbPath, lifecycle, reasonCode, lastError string) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()
	_, err = db.Exec(
		`INSERT INTO sync_state (target_key, lifecycle, reason_code, last_error) VALUES ('cloud', ?, ?, ?)`,
		lifecycle, reasonCode, lastError,
	)
	if err != nil {
		t.Fatalf("insert sync_state: %v", err)
	}
}

func insertMutation(t *testing.T, dbPath string, acked bool) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()
	ackedAt := sql.NullString{}
	if acked {
		ackedAt = sql.NullString{String: "2024-01-01T00:00:00Z", Valid: true}
	}
	_, err = db.Exec(`INSERT INTO sync_mutations (acked_at) VALUES (?)`, ackedAt)
	if err != nil {
		t.Fatalf("insert mutation: %v", err)
	}
}

// TestQueryStatus_SyncStateHealthy: lifecycle=active, no unacked mutations → fully synced.
func TestQueryStatus_SyncStateHealthy(t *testing.T) {
	dbPath := createTestDB(t)
	createSyncTables(t, dbPath)
	insertSyncState(t, dbPath, "active", "", "")
	// All mutations acked.
	insertMutation(t, dbPath, true)
	insertMutation(t, dbPath, true)

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !st.SyncStatePresent {
		t.Error("expected SyncStatePresent=true")
	}
	if st.SyncLifecycle != "active" {
		t.Errorf("expected lifecycle=active, got %q", st.SyncLifecycle)
	}
	if st.IsDegraded() {
		t.Error("expected IsDegraded=false")
	}
	if st.UnackedMutations != 0 {
		t.Errorf("expected UnackedMutations=0, got %d", st.UnackedMutations)
	}
}

// TestQueryStatus_SyncStateDegraded: lifecycle=degraded, reason set → degraded verdict.
func TestQueryStatus_SyncStateDegraded(t *testing.T) {
	dbPath := createTestDB(t)
	createSyncTables(t, dbPath)
	insertSyncState(t, dbPath, "degraded", "non_enrolled_pending_mutations", "")
	insertMutation(t, dbPath, false)
	insertMutation(t, dbPath, false)

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !st.SyncStatePresent {
		t.Error("expected SyncStatePresent=true")
	}
	if st.SyncLifecycle != "degraded" {
		t.Errorf("expected lifecycle=degraded, got %q", st.SyncLifecycle)
	}
	if !st.IsDegraded() {
		t.Error("expected IsDegraded=true")
	}
	if st.ReasonCode != "non_enrolled_pending_mutations" {
		t.Errorf("expected reason_code=non_enrolled_pending_mutations, got %q", st.ReasonCode)
	}
	if st.UnackedMutations != 2 {
		t.Errorf("expected UnackedMutations=2, got %d", st.UnackedMutations)
	}
}

// TestQueryStatus_UnackedBacklog: lifecycle=active but unacked mutations present.
func TestQueryStatus_UnackedBacklog(t *testing.T) {
	dbPath := createTestDB(t)
	createSyncTables(t, dbPath)
	insertSyncState(t, dbPath, "active", "", "")
	insertMutation(t, dbPath, true)  // acked
	insertMutation(t, dbPath, false) // unacked
	insertMutation(t, dbPath, false) // unacked

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.IsDegraded() {
		t.Error("expected IsDegraded=false")
	}
	if st.UnackedMutations != 2 {
		t.Errorf("expected UnackedMutations=2, got %d", st.UnackedMutations)
	}
}

// TestQueryStatus_MissingSyncTables: db without sync_state/sync_mutations → graceful fallback.
func TestQueryStatus_MissingSyncTables(t *testing.T) {
	dbPath := createTestDB(t)
	// No sync tables created — simulates an older engram store.

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !st.DBExists {
		t.Error("expected DBExists=true")
	}
	if st.SyncStatePresent {
		t.Error("expected SyncStatePresent=false for db without sync_state table")
	}
	if st.IsDegraded() {
		t.Error("expected IsDegraded=false when sync_state absent")
	}
	if st.UnackedMutations != 0 {
		t.Errorf("expected UnackedMutations=0, got %d", st.UnackedMutations)
	}
}

// insertSyncStateWithUpdatedAt inserts a sync_state cloud row with an explicit updated_at value.
func insertSyncStateWithUpdatedAt(t *testing.T, dbPath, lifecycle, updatedAt string) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()
	var ua sql.NullString
	if updatedAt != "" {
		ua = sql.NullString{String: updatedAt, Valid: true}
	}
	_, err = db.Exec(
		`INSERT INTO sync_state (target_key, lifecycle, updated_at) VALUES ('cloud', ?, ?)`,
		lifecycle, ua,
	)
	if err != nil {
		t.Fatalf("insert sync_state: %v", err)
	}
}

// TestQueryStatus_LastSyncFromDB: cloud sync_state row with a known updated_at → LastSync populated.
func TestQueryStatus_LastSyncFromDB(t *testing.T) {
	dbPath := createTestDB(t)
	createSyncTables(t, dbPath)
	// Use a fixed timestamp in SQLite's datetime format.
	const ts = "2026-07-14 12:34:56"
	insertSyncStateWithUpdatedAt(t, dbPath, "healthy", ts)

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.LastSync == nil {
		t.Fatal("expected LastSync to be set, got nil")
	}
	want, _ := time.Parse("2006-01-02 15:04:05", ts)
	want = want.UTC()
	if !st.LastSync.Equal(want) {
		t.Errorf("LastSync = %v, want %v", *st.LastSync, want)
	}
}

// TestQueryStatus_LastSyncAbsent: cloud sync_state row with NULL updated_at → LastSync nil.
func TestQueryStatus_LastSyncAbsent(t *testing.T) {
	dbPath := createTestDB(t)
	createSyncTables(t, dbPath)
	insertSyncStateWithUpdatedAt(t, dbPath, "healthy", "") // empty → NULL

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.LastSync != nil {
		t.Errorf("expected LastSync=nil for NULL updated_at, got %v", *st.LastSync)
	}
}

// TestQueryStatus_NoSyncState_LastSync: no sync_state table → LastSync nil.
func TestQueryStatus_NoSyncState_LastSync(t *testing.T) {
	dbPath := createTestDB(t)
	// No sync tables — older engram store.

	st, err := QueryStatus(dbPath)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.LastSync != nil {
		t.Errorf("expected LastSync=nil when sync_state absent, got %v", *st.LastSync)
	}
}
