package watcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/state"
)

// mockSession records StopWorker calls; satisfies session.Manager.
type mockSession struct{ stopped []string }

func (m *mockSession) SpawnWorker(workspace, name, command string) error { return nil }
func (m *mockSession) StopWorker(workspace, name string) error {
	m.stopped = append(m.stopped, name)
	return nil
}
func (m *mockSession) GetWorkerCwd(workspace, name string) (string, error) { return "", nil }
func (m *mockSession) Close() error                                        { return nil }

// reapWorker is the single deterministic teardown: kill tmux, delete KV, drop the
// watch entry — uniformly for process and non-process workers.
func TestReapWorker_KillsAndRemovesEverything(t *testing.T) {
	dir := t.TempDir()
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(dir, "mr-watcher.yaml"))
	const name = "sapbert--proj-1abc"
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			name: {Name: name, Project: "proj", Repo: "g/proj", MR: 7, State: "post_merge"},
		}
	})
	kv := newMockKV()
	// A process (SWE) worker — the kind that used to be left to self-terminate.
	kv.setAgent(name, map[string]any{"state": "running", "process": "swe"})
	sess := &mockSession{}

	w := &MRWatcher{
		State:    mrState,
		KV:       kv,
		Session:  sess,
		Vignoble: &config.Vignoble{Name: "testv"},
	}

	w.reapWorker(name)

	if len(sess.stopped) != 1 || sess.stopped[0] != name {
		t.Errorf("expected tmux StopWorker(%q), got %v", name, sess.stopped)
	}
	if data, _ := kv.Get("pinard-agents", name); data != nil {
		t.Errorf("expected KV entry deleted, still present: %v", data)
	}
	mrState.Read(func(s *state.MRWatcherState) {
		if _, ok := s.Watched[name]; ok {
			t.Error("expected watch entry removed after reap")
		}
	})
}

// When the Watched map key is a runId (process worker path) but rec["name"] holds
// the real tmux session name, reapWorker must call StopWorker with rec["name"].
func TestReapWorker_UsesRealSessionNameFromKV(t *testing.T) {
	dir := t.TempDir()
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(dir, "mr-watcher.yaml"))
	const kvKey = "pinard-swe-228"        // agentID / runId — the Watched map key
	const realSession = "memory--pinard-22813e4bc" // actual tmux session name
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			kvKey: {Name: kvKey, Project: "pinard", Repo: "g/pinard", MR: 228, State: "post_merge"},
		}
	})
	kv := newMockKV()
	// KV key is the runId; "name" field holds the real session name.
	kv.setAgent(kvKey, map[string]any{"state": "running", "process": "swe", "name": realSession})
	sess := &mockSession{}

	w := &MRWatcher{
		State:    mrState,
		KV:       kv,
		Session:  sess,
		Vignoble: &config.Vignoble{Name: "testv"},
	}

	w.reapWorker(kvKey)

	// Must kill the real tmux session name, not the KV key.
	if len(sess.stopped) != 1 || sess.stopped[0] != realSession {
		t.Errorf("expected StopWorker(%q), got %v", realSession, sess.stopped)
	}
	// KV entry keyed by the original key must be deleted.
	if data, _ := kv.Get("pinard-agents", kvKey); data != nil {
		t.Errorf("expected KV entry deleted, still present: %v", data)
	}
	mrState.Read(func(s *state.MRWatcherState) {
		if _, ok := s.Watched[kvKey]; ok {
			t.Error("expected watch entry removed after reap")
		}
	})
}

// reapLiveSession must not kill conductor or régisseur windows.
func TestReapLiveSession_SkipsConductorAndReserved(t *testing.T) {
	sess := &mockSession{}
	o := &OrphanRecovery{
		Session:  sess,
		Vignoble: &config.Vignoble{Name: "testv"},
		KV:       newMockKV(),
	}

	// Directly call reapSessionByName (the inner guard) with reserved names.
	// conductor and régisseur windows must never be stopped.
	for _, reserved := range []string{"conductor", session.RegisseurWindow} {
		o.reapSessionByName(reserved)
	}

	if len(sess.stopped) != 0 {
		t.Errorf("expected no StopWorker calls for reserved windows, got %v", sess.stopped)
	}
}

// reapLiveSession kills a non-reserved session via StopWorker.
func TestReapLiveSession_KillsNonReservedSession(t *testing.T) {
	sess := &mockSession{}
	o := &OrphanRecovery{
		Session:  sess,
		Vignoble: &config.Vignoble{Name: "testv"},
		KV:       newMockKV(),
	}

	o.reapSessionByName("pinard--proj-a1b2c3d4")

	if len(sess.stopped) != 1 || sess.stopped[0] != "pinard--proj-a1b2c3d4" {
		t.Errorf("expected StopWorker(pinard--proj-a1b2c3d4), got %v", sess.stopped)
	}
}

// Sentinel-driven sweep: a run with a 999999 journal sentinel that has its MR
// merged is marked completed and reapLiveSession is invoked (no-op in test env
// since WorkerForRun finds no tmux session, but markRunCompleted is asserted).
func TestOrphanRecovery_SentinelDrivenSweep_ReapsCompletedRuns(t *testing.T) {
	dir := t.TempDir()

	// Create a run that looks orphaned: no RUN_COMPLETED, MR already merged.
	runID := "memory--swe-42"
	parcelle := "pinard"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)

	// Active but stuck at event-wait — no terminal sentinel yet.
	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "wait-for-event"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)

	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)

	// MR watcher knows this run's MR is in post_merge state.
	stateDir := filepath.Join(dir, "state")
	os.MkdirAll(stateDir, 0755)
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(stateDir, "mr-watcher.yaml"))
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			runID: {Name: runID, Project: "memory", MR: 42, State: "post_merge"},
		}
	})

	kv := newMockKV()
	sess := &mockSession{}

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "testv",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{}},
		},
		KV:      kv,
		MRState: mrState,
		Session: sess,
	}

	o.Run()

	// The run must be marked finished (sentinel written).
	if !o.isRunFinished(runDir) {
		t.Error("run with merged MR should be marked finished")
	}
	// In test env WorkerForRun returns "" (no tmux), so StopWorker is not called.
	// The key assertion: markRunCompleted fired (sentinel written), not that tmux was killed.
	if !hasOrphanRecoveryEntry(t, journalDir) {
		t.Error("expected orphan-recovery sentinel written for merged-MR run")
	}
}

// Sentinel-less runs (no 999999 journal entry, no merged MR) must never be reaped.
func TestOrphanRecovery_SentinelDrivenSweep_SkipsSentinellessRuns(t *testing.T) {
	dir := t.TempDir()

	// Create a run with no terminal sentinel and an open MR.
	runID := "long-pipeline-run-99"
	parcelle := "compute"
	runDir := filepath.Join(dir, "parcelles", parcelle, "runs", runID)
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0755)

	event := map[string]any{"type": "EFFECT_REQUESTED", "data": map[string]any{"taskId": "run-pipeline"}}
	data, _ := json.Marshal(event)
	os.WriteFile(filepath.Join(journalDir, "000001.json"), data, 0644)

	runMeta := map[string]any{"processId": "swe", "runId": runID}
	metaData, _ := json.Marshal(runMeta)
	os.WriteFile(filepath.Join(runDir, "run.json"), metaData, 0644)

	// No matching MR in watcher state, no GitLab client (returns "").
	stateDir := filepath.Join(dir, "state")
	os.MkdirAll(stateDir, 0755)
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(stateDir, "mr-watcher.yaml"))

	kv := newMockKV()
	sess := &mockSession{}

	o := &OrphanRecovery{
		Vignoble: &config.Vignoble{
			Path:   dir,
			Name:   "testv",
			Config: &config.VignobleConfig{Vignes: map[string]config.Vigne{}},
		},
		KV:      kv,
		MRState: mrState,
		Session: sess,
	}

	o.Run()

	// The run must NOT be marked finished (no sentinel, no merged MR).
	if o.isRunFinished(runDir) {
		t.Error("run with no sentinel and open MR must not be reaped")
	}
	// No StopWorker must have been called.
	if len(sess.stopped) != 0 {
		t.Errorf("expected no StopWorker for sentinel-less run, got %v", sess.stopped)
	}
}

// A remote worker (no local tmux session) reaps gracefully: StopWorker is a no-op
// at the tmux layer but tracking state is still cleaned up.
func TestReapWorker_RemoteCleansTrackingState(t *testing.T) {
	dir := t.TempDir()
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(dir, "mr-watcher.yaml"))
	const name = "hpc--gwas-2xyz"
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{name: {Name: name, Project: "gwas", MR: 3}}
	})
	kv := newMockKV()
	kv.setAgent(name, map[string]any{"state": "running", "process": "swe", "location": "remote"})

	w := &MRWatcher{
		State:    mrState,
		KV:       kv,
		Session:  &mockSession{},
		Vignoble: &config.Vignoble{Name: "testv"},
	}

	w.reapWorker(name)

	if data, _ := kv.Get("pinard-agents", name); data != nil {
		t.Errorf("expected KV entry cleaned, still present: %v", data)
	}
	mrState.Read(func(s *state.MRWatcherState) {
		if _, ok := s.Watched[name]; ok {
			t.Error("expected watch entry removed after reap")
		}
	})
}
