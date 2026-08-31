package watcher

import (
	"path/filepath"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/state"
)

// mockKVStore implements pnats.KVWriter for testing
type mockKVStore struct {
	data map[string]map[string]map[string]any // bucket -> key -> value
}

func newMockKV() *mockKVStore {
	return &mockKVStore{data: make(map[string]map[string]map[string]any)}
}

func (m *mockKVStore) Get(bucket, key string) (map[string]any, error) {
	if m.data[bucket] == nil {
		return nil, nil
	}
	return m.data[bucket][key], nil
}

func (m *mockKVStore) Keys(bucket string) ([]string, error) {
	if m.data[bucket] == nil {
		return nil, nil
	}
	keys := make([]string, 0, len(m.data[bucket]))
	for k := range m.data[bucket] {
		keys = append(keys, k)
	}
	return keys, nil
}

func (m *mockKVStore) Put(bucket, key string, value any) error {
	if m.data[bucket] == nil {
		m.data[bucket] = make(map[string]map[string]any)
	}
	if v, ok := value.(map[string]any); ok {
		m.data[bucket][key] = v
	}
	return nil
}

func (m *mockKVStore) Del(bucket, key string) error {
	if m.data[bucket] != nil {
		delete(m.data[bucket], key)
	}
	return nil
}

func (m *mockKVStore) setAgent(key string, data map[string]any) {
	if m.data["pinard-agents"] == nil {
		m.data["pinard-agents"] = make(map[string]map[string]any)
	}
	m.data["pinard-agents"][key] = data
}

// --- Tests for getWorkerProcess ---

func TestGetWorkerProcess_ProcessWorker(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"process": "swe",
		"state":   "running",
	})

	w := &MRWatcher{KV: kv}
	got := w.getWorkerProcess("pinard-swe-42")
	if got != "swe" {
		t.Errorf("expected 'swe', got %q", got)
	}
}

func TestGetWorkerProcess_FreeformWorker(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("exohub-exo-cli-1234", map[string]any{
		"project": "exo-cli",
		"state":   "running",
	})

	w := &MRWatcher{KV: kv}
	got := w.getWorkerProcess("exohub-exo-cli-1234")
	if got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

func TestGetWorkerProcess_NoKVEntry(t *testing.T) {
	kv := newMockKV()
	w := &MRWatcher{KV: kv}
	got := w.getWorkerProcess("nonexistent")
	if got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

// --- Tests for findAgentForMR ---

func TestFindAgentForMR_Found(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"process": "swe",
	})
	kv.setAgent("other-worker", map[string]any{
		"project": "exo-cli",
		"process": "swe",
	})

	w := &MRWatcher{KV: kv}
	got := w.findAgentForMR("pinard", 0)
	if got != "pinard-swe-42" {
		t.Errorf("expected 'pinard-swe-42', got %q", got)
	}
}

func TestFindAgentForMR_NotFound(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("other-worker", map[string]any{
		"project": "exo-cli",
		"process": "swe",
	})

	w := &MRWatcher{KV: kv}
	got := w.findAgentForMR("pinard", 0)
	if got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

func TestFindAgentForMR_SkipsFreeformWorkers(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("freeform-worker", map[string]any{
		"project": "pinard",
		// no "process" field
	})

	w := &MRWatcher{KV: kv}
	got := w.findAgentForMR("pinard", 0)
	if got != "" {
		t.Errorf("expected empty (no process), got %q", got)
	}
}

// --- Tests for exact MR→worker routing (two workers, one repo) ---

// Regression: a comment on MR1 (owned by W1) must NOT route to W2, even though
// both workers share the same project/process. findAgentForMR must match the
// exact MR, not just the first process worker for the project.
func TestFindAgentForMR_ExactMatch_TwoWorkersOneRepo(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-w1", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(1), // JSON decodes as float64
	})
	kv.setAgent("pinard-swe-w2", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(2),
	})

	w := &MRWatcher{KV: kv}

	if got := w.findAgentForMR("pinard", 1); got != "pinard-swe-w1" {
		t.Errorf("MR 1 should resolve to W1, got %q", got)
	}
	if got := w.findAgentForMR("pinard", 2); got != "pinard-swe-w2" {
		t.Errorf("MR 2 should resolve to W2, got %q", got)
	}
}

// When no worker claims the exact MR, do NOT guess — return empty so the dispatch
// path won't misroute to an unrelated worker on the same repo.
func TestFindAgentForMR_ExactMatch_NoClaimReturnsEmpty(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-w1", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(1),
	})

	w := &MRWatcher{KV: kv}
	if got := w.findAgentForMR("pinard", 99); got != "" {
		t.Errorf("unclaimed MR 99 must not resolve to any worker, got %q", got)
	}
}

// Tolerate int / string MR encodings in KV (not just JSON float64).
func TestKvMRMatches_Forms(t *testing.T) {
	cases := []struct {
		v    any
		mr   int
		want bool
	}{
		{float64(42), 42, true},
		{int(42), 42, true},
		{"42", 42, true},
		{float64(42), 7, false},
		{nil, 42, false},
		{float64(0), 0, false}, // mrIID 0 never matches
	}
	for _, c := range cases {
		if got := kvMRMatches(c.v, c.mr); got != c.want {
			t.Errorf("kvMRMatches(%v, %d) = %v, want %v", c.v, c.mr, got, c.want)
		}
	}
}

// --- Tests for WorkerInboxSubject ---

func TestDispatch_InboxSubject_Process(t *testing.T) {
	got := WorkerInboxSubject("exohub", "semantic-search", "pinard-swe-42", "swe")
	expected := "pinard.exohub.parcelles.semantic-search.agents.pinard-swe-42.process.swe.inbox"
	if got != expected {
		t.Errorf("expected %q, got %q", expected, got)
	}
}

func TestDispatch_InboxSubject_Freeform(t *testing.T) {
	got := WorkerInboxSubject("exohub", "exo-cli", "exohub-exo-cli-1234", "")
	expected := "pinard.exohub.parcelles.exo-cli.agents.exohub-exo-cli-1234.inbox"
	if got != expected {
		t.Errorf("expected %q, got %q", expected, got)
	}
}

// --- Tests for scanAssignedMRs dedup ---

func TestScanAssignedMRs_SkipsAlreadyTracked(t *testing.T) {
	dir := t.TempDir()
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(dir, "mr-watcher.yaml"))

	// Pre-populate with an existing entry for MR !42
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			"pinard-swe-42": {
				Name:    "pinard-swe-42",
				Project: "exo-cli",
				Repo:    "exohub/exo-cli",
				MR:      42,
			},
		}
	})

	// Verify the entry exists
	var count int
	mrState.Read(func(s *state.MRWatcherState) {
		count = len(s.Watched)
	})
	if count != 1 {
		t.Fatalf("expected 1 watched entry, got %d", count)
	}
}

// --- Tests for dispatch routing ---

func TestDispatchRouting_ProcessWorkerGetsProcessInbox(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"process": "swe",
		"state":   "running",
	})

	w := &MRWatcher{
		KV: kv,
		Vignoble: &config.Vignoble{
			Name: "misc",
		},
	}

	processName := w.getWorkerProcess("pinard-swe-42")
	subject := WorkerInboxSubject(w.Vignoble.Name, "pinard", "pinard-swe-42", processName)
	expected := "pinard.misc.parcelles.pinard.agents.pinard-swe-42.process.swe.inbox"
	if subject != expected {
		t.Errorf("expected %q, got %q", expected, subject)
	}
}

func TestDispatchRouting_FreeformWorkerGetsFlatInbox(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("exohub-exo-cli-1234", map[string]any{
		"project": "exo-cli",
		"state":   "running",
	})

	w := &MRWatcher{
		KV: kv,
		Vignoble: &config.Vignoble{
			Name: "exohub",
		},
	}

	processName := w.getWorkerProcess("exohub-exo-cli-1234")
	subject := WorkerInboxSubject(w.Vignoble.Name, "exo-cli", "exohub-exo-cli-1234", processName)
	expected := "pinard.exohub.parcelles.exo-cli.agents.exohub-exo-cli-1234.inbox"
	if subject != expected {
		t.Errorf("expected %q, got %q", expected, subject)
	}
}

func TestDispatchRouting_FallbackFindsAgentFromKV(t *testing.T) {
	kv := newMockKV()
	// Worker registered under run ID, but tracked under track-pinard-42
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"process": "swe",
		"state":   "running",
	})

	w := &MRWatcher{
		KV: kv,
		Vignoble: &config.Vignoble{
			Name: "misc",
		},
	}

	// Simulate dispatch for a track-* entry that has no KV
	session := "track-pinard-42"
	processName := w.getWorkerProcess(session)
	if processName != "" {
		t.Fatal("track- key should not have a KV entry")
	}

	// Fallback: find agent by project
	agentID := w.findAgentForMR("pinard", 0)
	if agentID == "" {
		t.Fatal("should find pinard-swe-42 from KV")
	}

	altProcess := w.getWorkerProcess(agentID)
	subject := WorkerInboxSubject(w.Vignoble.Name, "pinard", agentID, altProcess)
	expected := "pinard.misc.parcelles.pinard.agents.pinard-swe-42.process.swe.inbox"
	if subject != expected {
		t.Errorf("expected %q, got %q", expected, subject)
	}
}

// BUG REGRESSION (#63): when a tracked entry has project:"" (written before the
// fix), forwardNotes produces an event with project:"". dispatchToWorkerWithType
// must still route correctly by falling back to findAgentForMRByID (MR-only scan).
func TestFindAgentForMRByID_FoundByMRAlone(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-63", map[string]any{
		"project":  "pinard",
		"process":  "swe",
		"mr":       float64(63),
		"vignoble": "misc",
	})

	w := &MRWatcher{
		KV: kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	got := w.findAgentForMRByID(63)
	if got != "pinard-swe-63" {
		t.Errorf("expected 'pinard-swe-63', got %q", got)
	}
}

func TestFindAgentForMRByID_SkipsFreeformWorkers(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("memory--pinard-60a2f57b", map[string]any{
		"project": "pinard",
		"mr":      float64(42),
		// no process — freeform worker
	})

	w := &MRWatcher{
		KV:       kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	got := w.findAgentForMRByID(42)
	if got != "" {
		t.Errorf("expected empty (no process), got %q", got)
	}
}

// BUG REGRESSION: when two workers in different repos both have MR !N stamped,
// findAgentForMRByID must return "" (ambiguous) instead of the first match.
// The misc vignoble watches mnemosyne, pinard, artifactdb-xp, litsum-workers,
// litsum-api — each with independent MR numbering.
func TestFindAgentForMRByIDAndRepo_CrossRepoCollision_Ambiguous(t *testing.T) {
	kv := newMockKV()
	// Two workers in different repos, same MR IID
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(42),
		"repo": "your-group/pinard", "vignoble": "misc",
	})
	kv.setAgent("mnemosyne-swe-42", map[string]any{
		"project": "mnemosyne", "process": "swe", "mr": float64(42),
		"repo": "huge/mnemosyne", "vignoble": "misc",
	})

	w := &MRWatcher{
		KV:       kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	// No repo hint → ambiguous → must return ""
	if got := w.findAgentForMRByID(42); got != "" {
		t.Errorf("ambiguous MR !42 (two repos) must return empty, got %q", got)
	}
}

func TestFindAgentForMRByIDAndRepo_CrossRepoCollision_RepoTiebreak(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(42),
		"repo": "your-group/pinard", "vignoble": "misc",
	})
	kv.setAgent("mnemosyne-swe-42", map[string]any{
		"project": "mnemosyne", "process": "swe", "mr": float64(42),
		"repo": "huge/mnemosyne", "vignoble": "misc",
	})

	w := &MRWatcher{
		KV:       kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	// With repo hint → disambiguated to the pinard worker
	got := w.findAgentForMRByIDAndRepo(42, "your-group/pinard")
	if got != "pinard-swe-42" {
		t.Errorf("repo tiebreak should resolve to pinard-swe-42, got %q", got)
	}

	// With mnemosyne repo hint → disambiguated to the mnemosyne worker
	got = w.findAgentForMRByIDAndRepo(42, "huge/mnemosyne")
	if got != "mnemosyne-swe-42" {
		t.Errorf("repo tiebreak should resolve to mnemosyne-swe-42, got %q", got)
	}
}

func TestFindAgentForMRByIDAndRepo_CrossRepoCollision_BothSameRepo(t *testing.T) {
	kv := newMockKV()
	// Two workers in the SAME repo with the same MR — still ambiguous
	kv.setAgent("pinard-swe-w1", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(42),
		"repo": "your-group/pinard", "vignoble": "misc",
	})
	kv.setAgent("pinard-swe-w2", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(42),
		"repo": "your-group/pinard", "vignoble": "misc",
	})

	w := &MRWatcher{
		KV:       kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	if got := w.findAgentForMRByIDAndRepo(42, "your-group/pinard"); got != "" {
		t.Errorf("two workers same repo same MR must return empty (ambiguous), got %q", got)
	}
}

func TestFindAgentForMRByID_NoClaim(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-99", map[string]any{
		"project": "pinard", "process": "swe", "mr": float64(99),
	})

	w := &MRWatcher{
		KV:       kv,
		Vignoble: &config.Vignoble{Name: "misc"},
	}

	got := w.findAgentForMRByID(42)
	if got != "" {
		t.Errorf("expected empty for unclaimed MR 42, got %q", got)
	}
}

// Single-entry invariant: even when the tracked entry for a session has
// project:"", scanAssignedMRs must NOT mint a second tracking-only entry for the
// same MR (dedup is by MR IID, not by project).
func TestScanAssignedMRs_SingleEntryInvariant_EmptyProject(t *testing.T) {
	path := t.TempDir() + "/mr-watcher.yaml"
	mrState, _ := state.Load[state.MRWatcherState](path)

	// Simulate a pre-fix tracked entry with empty project
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			"memory--pinard-60a2f57b": {
				Name:    "memory--pinard-60a2f57b",
				Project: "", // broken state from pre-fix track-mr
				Repo:    "your-group/pinard",
				MR:      42,
			},
		}
	})

	// Dedup logic in scanAssignedMRs: check if the MR is already tracked
	// (should be true — entry.MR == 42 regardless of project)
	mrIID := 42
	alreadyTracked := false
	mrState.Read(func(s *state.MRWatcherState) {
		for _, entry := range s.Watched {
			if entry.MR == mrIID {
				alreadyTracked = true
				return
			}
		}
	})
	if !alreadyTracked {
		t.Error("MR 42 should be detected as already tracked even with project:\"\"")
	}

	// The state should still have exactly 1 entry
	var count int
	mrState.Read(func(s *state.MRWatcherState) { count = len(s.Watched) })
	if count != 1 {
		t.Errorf("expected 1 tracked entry, got %d (double-tracking detected!)", count)
	}
}

// --- Tests for pipeline dispatch with process workers ---

func TestDispatchRouting_ProcessWorkerAlwaysDispatched(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("pinard-swe-42", map[string]any{
		"project": "pinard",
		"process": "swe",
		"state":   "running",
	})

	w := &MRWatcher{KV: kv}

	// Process worker should always be dispatched (alive check bypassed)
	processName := w.getWorkerProcess("pinard-swe-42")
	if processName == "" {
		t.Error("process worker should have non-empty process name")
	}
}
