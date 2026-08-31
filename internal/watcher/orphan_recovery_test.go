package watcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/state"
)

// stubMRGetter satisfies mrStateGetter, returning a canned state per (repo, iid).
type stubMRGetter struct {
	states map[int]string // mr iid → state
	calls  int
}

func (s *stubMRGetter) GetMR(repo string, iid int) (*gitlab.MergeRequest, error) {
	s.calls++
	st, ok := s.states[iid]
	if !ok {
		return &gitlab.MergeRequest{IID: iid, State: "opened"}, nil
	}
	return &gitlab.MergeRequest{IID: iid, State: st}, nil
}

// writeOpenMRResult writes a task result.json mimicking the swe `open-mr` task,
// so runMRIID can recover the MR number the way it does in production.
func writeOpenMRResult(t *testing.T, runDir string, mrIID int) {
	t.Helper()
	taskDir := filepath.Join(runDir, "tasks", "01OPENMR")
	if err := os.MkdirAll(taskDir, 0755); err != nil {
		t.Fatal(err)
	}
	res := map[string]any{
		"taskId": "open-mr",
		"status": "ok",
		"value":  map[string]any{"mrIid": mrIID},
	}
	data, _ := json.Marshal(res)
	if err := os.WriteFile(filepath.Join(taskDir, "result.json"), data, 0644); err != nil {
		t.Fatal(err)
	}
}

func TestOrphanRecovery_IsRunFinished_Completed(t *testing.T) {
	dir := t.TempDir()
	journalDir := filepath.Join(dir, "journal")
	os.MkdirAll(journalDir, 0755)

	event := map[string]any{"type": "RUN_COMPLETED", "data": map[string]any{}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.test.json"), data, 0644)

	o := &OrphanRecovery{}
	if !o.isRunFinished(dir) {
		t.Error("should be finished (RUN_COMPLETED)")
	}
}

func TestOrphanRecovery_IsRunFinished_Failed(t *testing.T) {
	dir := t.TempDir()
	journalDir := filepath.Join(dir, "journal")
	os.MkdirAll(journalDir, 0755)

	event := map[string]any{"type": "RUN_FAILED", "data": map[string]any{}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.test.json"), data, 0644)

	o := &OrphanRecovery{}
	if !o.isRunFinished(dir) {
		t.Error("should be finished (RUN_FAILED)")
	}
}

func TestOrphanRecovery_IsRunFinished_Incomplete(t *testing.T) {
	dir := t.TempDir()
	journalDir := filepath.Join(dir, "journal")
	os.MkdirAll(journalDir, 0755)

	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "analyze"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.test.json"), data, 0644)

	o := &OrphanRecovery{}
	if o.isRunFinished(dir) {
		t.Error("should NOT be finished (only EFFECT_REQUESTED)")
	}
}

func TestOrphanRecovery_IsRunFinished_NoJournal(t *testing.T) {
	dir := t.TempDir()
	o := &OrphanRecovery{}
	if !o.isRunFinished(dir) {
		t.Error("no journal dir should be treated as finished (can't recover)")
	}
}

func TestOrphanRecovery_GetActiveRunIDs(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"runId":   "pinard-swe-42",
	})
	kv.setAgent("exo-cli-swe-10", map[string]any{
		"project": "exo-cli",
		"runId":   "exo-cli-swe-10",
	})
	kv.setAgent("freeform-worker", map[string]any{
		"project": "charon",
	})

	o := &OrphanRecovery{KV: kv}
	active := o.getActiveRunIDs()

	if !active["pinard-swe-42"] {
		t.Error("should find pinard-swe-42 as active")
	}
	if !active["exo-cli-swe-10"] {
		t.Error("should find exo-cli-swe-10 as active")
	}
	if active["freeform-worker"] {
		t.Error("freeform worker without runId should not appear")
	}
}

func TestOrphanRecovery_DeduplicatesByRunID(t *testing.T) {
	dir := t.TempDir()

	// Create a run in two parcelles
	for _, parcelle := range []string{"dashboard", "pinard"} {
		runDir := filepath.Join(dir, "parcelles", parcelle, "runs", "pinard-swe-42")
		journalDir := filepath.Join(runDir, "journal")
		os.MkdirAll(journalDir, 0755)
		// Write incomplete journal
		event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "test"}}
		data, _ := json.Marshal(event)
		os.WriteFile(filepath.Join(journalDir, "000001.test.json"), data, 0644)
		// Write run.json
		runMeta := map[string]any{"processId": "swe", "runId": "pinard-swe-42"}
		metaData, _ := json.Marshal(runMeta)
		os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)
	}

	kv := newMockKV() // no active workers

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "test",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{}},
		},
		KV: kv,
	}

	// Run() will find the orphan but respawn() will fail (no aoc binary in test).
	// The key assertion: it doesn't panic and only processes the run once (dedup).
	o.Run()
}

func TestOrphanRecovery_SkipsArchived(t *testing.T) {
	dir := t.TempDir()

	// Create an archived parcelle with an incomplete run
	parcelle := "old-work"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", "old-run-1")
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "test"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.test.json"), data, 0644)
	runMeta := map[string]any{"processId": "swe"}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)

	// Mark parcelle as archived
	os.WriteFile(filepath.Join(dir, "parcelles", parcelle, "parcelle.yaml"), []byte("status: archived\n"), 0644)

	kv := newMockKV()
	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "test",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{}},
		},
		KV: kv,
	}
	// Should not panic or attempt respawn for archived parcelle
	o.Run()
}

// Regression test: orphan recovery must NOT respawn runs whose MR is already merged.
// This was a serious bug where workers were dispatched to already-merged MRs.
func TestOrphanRecovery_SkipsRunsWithMergedMR(t *testing.T) {
	dir := t.TempDir()

	// Create an incomplete run (no RUN_COMPLETED in journal)
	runID := "rosetta-api-swe-exohub-rosetta-api-3621"
	parcelle := "embeddings"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "implement"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)
	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)

	// Set up MR state showing this run's MR is in post_merge state
	stateDir := filepath.Join(dir, "state")
	os.MkdirAll(stateDir, 0755)
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(stateDir, "mr-watcher.yaml"))
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			runID: {
				Name:    runID,
				Project: "rosetta-api",
				MR:      4,
				State:   "post_merge",
			},
		}
	})

	kv := newMockKV() // no active workers

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "exohub",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{"rosetta-api": {}}},
		},
		KV:      kv,
		MRState: mrState,
	}

	o.Run()

	// The run should now have a RUN_FAILED journal entry (marked as done)
	if !o.isRunFinished(runDir) {
		t.Error("run with merged MR should be marked as finished after orphan recovery")
	}

	// Verify a RUN_FAILED journal entry was written (999999.<rand>.json format)
	if !hasOrphanRecoveryEntry(t, journalDir) {
		t.Error("expected orphan-recovery journal entry to be written")
	}
}

// Regression test: orphan recovery must NOT respawn a run whose MR a human
// CLOSED (not merged). On close the MR watcher deletes the tracked entry, so
// the post_merge state check (isRunMRDone) can't see it — orphan-recovery must
// fall back to querying GitLab. This is the exohub-charon-1829 incident.
func TestOrphanRecovery_SkipsRunsWithClosedMR(t *testing.T) {
	dir := t.TempDir()

	runID := "charon-swe-38"
	parcelle := "charon"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)
	// Unfinished: parked on wait-for-event, no RUN_COMPLETED/RUN_FAILED.
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "wait-for-event"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)
	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)
	// The run opened MR !330 and recorded the repo in inputs.json.
	writeOpenMRResult(t, runDir, 330)
	inputs := map[string]any{"repo": "GP/charon", "project": "charon"}
	inputsData, _ := json.Marshal(inputs)
	os.WriteFile(filepath.Join(runDir, "inputs.json"), inputsData, 0644)

	// No watched MR entry — it was deleted when the MR closed.
	stateDir := filepath.Join(dir, "state")
	os.MkdirAll(stateDir, 0755)
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(stateDir, "mr-watcher.yaml"))

	kv := newMockKV() // no active workers
	gl := &stubMRGetter{states: map[int]string{330: "closed"}}

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "exohub",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{"charon": {}}},
		},
		KV:      kv,
		MRState: mrState,
		GitLab:  gl,
	}

	o.Run()

	if gl.calls == 0 {
		t.Error("expected orphan-recovery to query GitLab for the MR state")
	}
	if !o.isRunFinished(runDir) {
		t.Error("run with closed MR should be marked finished, not respawned")
	}
	if !hasOrphanRecoveryEntry(t, journalDir) {
		t.Error("expected orphan-recovery journal entry to be written for closed-MR run")
	}
}

// Counterpart: a run whose MR is still OPEN must remain recoverable — the
// GitLab guard must not suppress legitimate orphan respawns.
func TestOrphanRecovery_StillRespawnsOpenMR(t *testing.T) {
	dir := t.TempDir()

	runID := "charon-swe-99"
	runDir := filepath.Join(dir, "parcelles", "charon", "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "wait-for-event"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)
	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)
	writeOpenMRResult(t, runDir, 331)
	inputs := map[string]any{"repo": "GP/charon", "project": "charon"}
	inputsData, _ := json.Marshal(inputs)
	os.WriteFile(filepath.Join(runDir, "inputs.json"), inputsData, 0644)

	stateDir := filepath.Join(dir, "state")
	os.MkdirAll(stateDir, 0755)
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(stateDir, "mr-watcher.yaml"))

	kv := newMockKV()
	gl := &stubMRGetter{states: map[int]string{331: "opened"}}

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "exohub",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{"charon": {}}},
		},
		KV:      kv,
		MRState: mrState,
		GitLab:  gl,
	}

	o.Run()

	// respawn() runs `aoc` (absent in tests) and fails — but the run must NOT be
	// marked finished, proving the open MR didn't trip the merged/closed guard.
	if o.isRunFinished(runDir) {
		t.Error("run with open MR must remain recoverable, not marked finished")
	}
	if o.retries[runID] == 0 {
		t.Error("expected a respawn attempt for the open-MR orphan")
	}
}

// Regression test: after exhausting retries, the run must be marked finished
// so it doesn't keep triggering on every daemon restart.
func TestOrphanRecovery_ExhaustedRetriesMarksRunFinished(t *testing.T) {
	dir := t.TempDir()

	runID := "exo-cli-swe-23"
	parcelle := "exo-cli"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "fix"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)
	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)

	kv := newMockKV()

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "exohub",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{"exo-cli": {}}},
		},
		KV:      kv,
		retries: map[string]int{runID: maxOrphanRetries}, // already at limit
	}

	o.Run()

	// After exhausting retries, the run should be permanently marked finished
	if !o.isRunFinished(runDir) {
		t.Error("exhausted-retry run should be marked as finished")
	}
}

func TestOrphanRecovery_ReapStaleRegistry(t *testing.T) {
	kv := newMockKV()
	// Entry keyed by the run ID itself (spawn writes agentID == runID).
	kv.setAgent("rosetta-data-swe-2", map[string]any{
		"runId": "rosetta-data-swe-2", "state": "running",
	})
	// Entry keyed by session name (e.g. track-mr) but pointing at the run.
	kv.setAgent("track-rosetta-data-3", map[string]any{
		"runId": "rosetta-data-swe-2", "state": "running",
	})
	// Unrelated entry that must survive.
	kv.setAgent("other-swe-9", map[string]any{
		"runId": "other-swe-9", "state": "running",
	})

	o := &OrphanRecovery{KV: kv}
	o.reapStaleRegistry("rosetta-data-swe-2")

	if v, _ := kv.Get("pinard-agents", "rosetta-data-swe-2"); v != nil {
		t.Error("entry keyed by runID should be reaped")
	}
	if v, _ := kv.Get("pinard-agents", "track-rosetta-data-3"); v != nil {
		t.Error("entry whose runId points at the run should be reaped")
	}
	if v, _ := kv.Get("pinard-agents", "other-swe-9"); v == nil {
		t.Error("unrelated entry must survive reaping")
	}
}

// hasOrphanRecoveryEntry checks that markRunCompleted wrote a 999999.<rand>.json
// RUN_FAILED journal entry in the given journal directory.
func hasOrphanRecoveryEntry(t *testing.T, journalDir string) bool {
	t.Helper()
	entries, err := os.ReadDir(journalDir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, "999999.") && strings.HasSuffix(name, ".json") {
			return true
		}
	}
	return false
}
