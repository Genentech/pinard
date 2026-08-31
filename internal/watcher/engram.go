package watcher

import (
	"database/sql"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/engram"
	"github.com/Genentech/pinard/internal/pnats"
	_ "modernc.org/sqlite"
)

// EngramSyncer replicates a vignoble's local-first engram store to the central
// `engram cloud serve` backend. This was previously a per-session bash loop in
// bin/pinard; the always-on daemon is the proper owner on the pinard host.
//
// Scope is per-vignoble (matching bin/pinard): DataDir = <vignoble>/.engram,
// Project = vignoble name. The daemon drains the shared local db; per-write
// replication (ENGRAM_CLOUD_AUTOSYNC) still runs inside each pi session, and
// standalone HPC workers (no daemon) keep their own bash loop.
type EngramSyncer struct {
	Server   string // engram cloud backend, e.g. https://engram.example.com
	Token    string // shared ENGRAM_CLOUD_TOKEN
	Project  string // engram project = vignoble name
	DataDir  string // ENGRAM_DATA_DIR = <vignoble>/.engram
	KV       *pnats.KV
	Vignoble string // vignoble name (used as KV key)
	bin      string
}

// Enabled reports whether cloud sync should run: engram binary present + a cloud
// token, server, and project configured. Absent → engram stays purely local.
func (e *EngramSyncer) Enabled() bool {
	if e.Server == "" || e.Token == "" || e.Project == "" {
		return false
	}
	p, err := exec.LookPath("engram")
	if err != nil {
		return false
	}
	e.bin = p
	return true
}

// Interval is the periodic drain cadence (default 5m; override with
// ENGRAM_SYNC_INTERVAL in seconds, matching the old bash loop's knob).
func (e *EngramSyncer) Interval() time.Duration {
	if s := os.Getenv("ENGRAM_SYNC_INTERVAL"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return 5 * time.Minute
}

func (e *EngramSyncer) exec(args ...string) error {
	cmd := exec.Command(e.bin, args...)
	// ENGRAM_DATA_DIR scopes the store to this vignoble; ENGRAM_CLOUD_TOKEN
	// authenticates to the backend (server is persisted by `cloud config`).
	env := append(os.Environ(),
		"ENGRAM_DATA_DIR="+e.DataDir,
		"ENGRAM_CLOUD_TOKEN="+e.Token,
	)
	// Inject a placeholder GITHUB_TOKEN so engram's GitHub update-check gets a
	// fast 401 instead of hanging until the daemon's 2-min tick timeout fires.
	// Only set it when neither GITHUB_TOKEN nor GH_TOKEN is already present so
	// we don't shadow a real token the operator has configured.
	if os.Getenv("GITHUB_TOKEN") == "" && os.Getenv("GH_TOKEN") == "" {
		env = append(env, "GITHUB_TOKEN=x")
	}
	cmd.Env = env
	out, err := cmd.CombinedOutput()
	outStr := strings.TrimSpace(string(out))
	if err != nil {
		// The engram CLI can exit non-zero when its GitHub update-check fails
		// (e.g. HTTP 403 rate-limit), even when the sync itself succeeded.
		// Treat as success only when a positive sync-success marker is present
		// AND the remaining output is solely update-check noise.
		// If there is no success marker the sync likely didn't run — log a
		// degraded-tick warning so the operator knows replication may be stalled.
		if hasSyncSuccessMarker(outStr) && onlyNonSuccessLinesAreUpdateCheckNoise(outStr) {
			log.Printf("[engram-sync] sync ok (update-check noise suppressed): %s", outStr)
			return nil
		}
		if onlyUpdateCheckNoise(outStr) {
			// Update-check failed and no success marker — sync likely did not run.
			log.Printf("[engram-sync] sync skipped by update-check failure, will retry: %s", outStr)
			return nil
		}
		return fmt.Errorf("%v: %s", err, outStr)
	}
	return nil
}

// hasSyncSuccessMarker reports whether the output contains a line that
// positively confirms the sync ran and completed.
func hasSyncSuccessMarker(output string) bool {
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if strings.Contains(line, "Nothing new to sync") ||
			strings.Contains(line, "exported") && strings.Contains(line, "to cloud") {
			return true
		}
	}
	return false
}

// onlyNonSuccessLinesAreUpdateCheckNoise reports whether every non-empty,
// non-success line in the output is an update-check noise line. Used together
// with hasSyncSuccessMarker to confirm "sync ran fine, update-check just noisy".
func onlyNonSuccessLinesAreUpdateCheckNoise(output string) bool {
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.Contains(line, "Nothing new to sync") ||
			(strings.Contains(line, "exported") && strings.Contains(line, "to cloud")) {
			continue // success marker line — fine
		}
		if !strings.Contains(line, "Could not check for updates:") {
			return false // unexpected non-noise, non-success line
		}
	}
	return true
}

// onlyUpdateCheckNoise reports whether all non-empty lines are GitHub
// update-check messages and nothing else (no success marker, no real error).
func onlyUpdateCheckNoise(output string) bool {
	if output == "" {
		return false
	}
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if !strings.Contains(line, "Could not check for updates:") {
			return false
		}
	}
	return true
}

// listProjectsWithData returns the distinct engram project names that have
// unacked mutations in the local db (i.e. data pending cloud push). This drives
// dynamic enrollment: any project that has written at least one memory will be
// enrolled on the next sync tick, with no config coupling.
func listProjectsWithData(dbPath string) ([]string, error) {
	if _, err := os.Stat(dbPath); err != nil {
		return nil, nil // db absent — no projects yet
	}
	dsn := fmt.Sprintf("file:%s?mode=ro&_journal_mode=WAL&cache=shared", dbPath)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open engram db: %w", err)
	}
	defer db.Close()

	// Check that sync_mutations exists (older engram stores may not have it).
	var exists int
	_ = db.QueryRow(
		`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sync_mutations'`,
	).Scan(&exists)
	if exists == 0 {
		return nil, nil
	}

	// Collect distinct projects with unacked mutations.
	rows, err := db.Query(`SELECT DISTINCT project FROM sync_mutations WHERE acked_at IS NULL AND project IS NOT NULL AND project != ''`)
	if err != nil {
		return nil, fmt.Errorf("query sync_mutations: %w", err)
	}
	defer rows.Close()
	var projects []string
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err == nil && p != "" {
			projects = append(projects, p)
		}
	}
	return projects, rows.Err()
}

// enrollAndSyncAll enrolls each project (idempotent) and drains its backlog.
// The baseline project (e.Project) is always included — it is enrolled even
// when the db has no data for it yet, ensuring the régisseur's slot is always
// reserved. Non-fatal: errors are logged but never stop the sync loop.
func (e *EngramSyncer) enrollAndSyncAll(projects []string) (lastErr error) {
	// Always include the baseline vignoble project.
	seen := map[string]bool{e.Project: false}
	for _, p := range projects {
		if p != "" {
			seen[p] = false
		}
	}
	for p := range seen {
		if err := e.exec("cloud", "enroll", p); err != nil {
			log.Printf("[engram-sync] enroll %q failed: %v", p, err)
			lastErr = err
		}
		if err := e.exec("sync", "--cloud", "--project", p); err != nil {
			log.Printf("[engram-sync] sync %q failed: %v", p, err)
			lastErr = err
		}
	}
	return lastErr
}

// Setup runs once at daemon start: point engram at the backend, enroll all
// projects that already have data (plus the baseline vignoble project), and
// drain their backlogs. All steps are non-fatal — an unreachable server / bad
// token never blocks the daemon; the local store stays the source of truth.
func (e *EngramSyncer) Setup() {
	if err := e.exec("cloud", "config", "--server", e.Server); err != nil {
		log.Printf("[engram-sync] cloud config failed — replication off, local memory unaffected: %v", err)
		e.writeKV(engram.SyncRecord{Result: "error", Error: err.Error()})
		return
	}
	dbPath := filepath.Join(e.DataDir, "engram.db")
	projects, err := listProjectsWithData(dbPath)
	if err != nil {
		log.Printf("[engram-sync] could not enumerate projects: %v", err)
	}
	if syncErr := e.enrollAndSyncAll(projects); syncErr != nil {
		log.Printf("[engram-sync] initial drain had errors — will retry on the next tick")
		e.writeKV(engram.SyncRecord{Result: "error", Error: syncErr.Error()})
		return
	}
	log.Printf("[engram-sync] → %s (initial drain ok; %d project(s); every %s)", e.Server, len(seen(e.Project, projects)), e.Interval())
	e.writeKVAfterSync()
}

// Run is the periodic drain (daemon ticker). Discovers all projects with data,
// enrolls any new ones (idempotent), and syncs each. Non-fatal.
func (e *EngramSyncer) Run() {
	dbPath := filepath.Join(e.DataDir, "engram.db")
	projects, err := listProjectsWithData(dbPath)
	if err != nil {
		log.Printf("[engram-sync] could not enumerate projects: %v", err)
	}
	if syncErr := e.enrollAndSyncAll(projects); syncErr != nil {
		log.Printf("[engram-sync] periodic sync had errors: %v", syncErr)
		e.writeKV(engram.SyncRecord{Result: "error", Error: syncErr.Error()})
		return
	}
	e.writeKVAfterSync()
}

// seen returns the deduplicated set of projects (baseline + data projects) for logging.
func seen(baseline string, projects []string) map[string]struct{} {
	set := map[string]struct{}{baseline: {}}
	for _, p := range projects {
		if p != "" {
			set[p] = struct{}{}
		}
	}
	return set
}

// writeKVAfterSync queries the local db for the aggregate pending count across
// all projects and writes a freshness record to NATS KV. Best-effort.
func (e *EngramSyncer) writeKVAfterSync() {
	dbPath := filepath.Join(e.DataDir, "engram.db")
	rec := engram.SyncRecord{
		LastSync: time.Now().UTC(),
		Result:   "ok",
	}
	if st, err := engram.QueryStatus(dbPath); err == nil && st.DBExists {
		rec.Pending = st.UnackedMutations
		rec.Degraded = st.IsDegraded()
		rec.ReasonCode = st.ReasonCode
	}
	e.writeKV(rec)
}

// writeKV persists the sync record to NATS KV (bucket "pinard-engram", key =
// vignoble name). Best-effort — logs and returns on any failure.
func (e *EngramSyncer) writeKV(rec engram.SyncRecord) {
	if e.KV == nil || e.Vignoble == "" {
		return
	}
	if err := e.KV.EnsureBucket("pinard-engram"); err != nil {
		log.Printf("[engram-sync] KV ensure bucket failed: %v", err)
		return
	}
	if err := e.KV.Put("pinard-engram", e.Vignoble, rec); err != nil {
		log.Printf("[engram-sync] KV put failed: %v", err)
	}
}
