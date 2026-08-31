package watcher

import (
	"path/filepath"
	"testing"

	"github.com/Genentech/pinard/internal/config"
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
