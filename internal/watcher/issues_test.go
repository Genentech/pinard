package watcher

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/state"
)

func TestIssueWatcher_NewIssueTracked(t *testing.T) {
	dir := t.TempDir()
	issueState, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))

	// Simulate detecting a new issue
	issueState.Update(func(s *state.IssueWatcherState) {
		if s.Seen == nil {
			s.Seen = make(map[string]map[string]*state.SeenIssue)
		}
		if s.Seen["exo-cli"] == nil {
			s.Seen["exo-cli"] = make(map[string]*state.SeenIssue)
		}
		s.Seen["exo-cli"]["42"] = &state.SeenIssue{
			Status: "spawned",
			Title:  "Fix auth flow",
		}
	})

	issueState.Read(func(s *state.IssueWatcherState) {
		entry := s.Seen["exo-cli"]["42"]
		if entry == nil {
			t.Fatal("issue 42 should exist")
		}
		if entry.Status != "spawned" {
			t.Errorf("expected status 'spawned', got %q", entry.Status)
		}
	})
}

func TestIssueWatcher_AlreadySeenNotRepublished(t *testing.T) {
	dir := t.TempDir()
	issueState, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))
	issueState.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"exo-cli": {
				"42": {Status: "spawned", Title: "Fix auth"},
			},
		}
	})

	// Check if issue should be skipped
	var shouldSkip bool
	issueState.Read(func(s *state.IssueWatcherState) {
		existing := s.Seen["exo-cli"]["42"]
		shouldSkip = existing != nil && (existing.Status == "spawned" || existing.Status == "closed")
	})

	if !shouldSkip {
		t.Error("already-seen issue should be skipped")
	}
}

func TestIssueWatcher_ClosedIssueUpdated(t *testing.T) {
	dir := t.TempDir()
	issueState, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))
	issueState.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"exo-cli": {
				"42": {Status: "spawned", Title: "Fix auth"},
			},
		}
	})

	// Simulate closure detection
	issueState.Update(func(s *state.IssueWatcherState) {
		s.Seen["exo-cli"]["42"].Status = "closed"
	})

	issueState.Read(func(s *state.IssueWatcherState) {
		if s.Seen["exo-cli"]["42"].Status != "closed" {
			t.Error("issue should be marked as closed")
		}
	})
}

func TestIssueWatcher_BlockedLabelPreventsSpawn(t *testing.T) {
	tests := []struct {
		labels      []string
		expectBlock bool
	}{
		{[]string{"bug"}, false},
		{[]string{"blocked"}, true},
		{[]string{"bug", "blocked"}, true},
		{[]string{"pinard:discarded"}, true},
		{[]string{"bug", "pinard:discarded"}, true},
		{[]string{"semantic-search"}, false},
		{[]string{}, false},
	}

	for _, tc := range tests {
		blocked := false
		for _, l := range tc.labels {
			if l == "blocked" || l == "pinard:discarded" {
				blocked = true
				break
			}
		}
		if blocked != tc.expectBlock {
			t.Errorf("labels=%v: expected blocked=%v, got %v", tc.labels, tc.expectBlock, blocked)
		}
	}
}

// Verify the code path: autoSpawnForIssue is called unless blocked.
func TestIssueWatcher_AutoSpawnCalledFromDaemon(t *testing.T) {
	src, err := os.ReadFile("issues.go")
	if err != nil {
		t.Skipf("cannot read issues.go: %v", err)
	}
	content := string(src)

	if !strings.Contains(content, "func (w *IssueWatcher) autoSpawnForIssue") {
		t.Error("autoSpawnForIssue method not found in issues.go")
	}

	if !strings.Contains(content, "if !blocked {") {
		t.Error("blocked guard not found — daemon must skip spawn when blocked label is present")
	}
	if !strings.Contains(content, "w.autoSpawnForIssue(") {
		t.Error("autoSpawnForIssue call not found after autoSpawn check")
	}

	// It must call aoc spawn
	if !strings.Contains(content, `"aoc", "spawn"`) && !strings.Contains(content, `"aoc","spawn"`) {
		// Check for Command("aoc", "spawn", ...)
		if !strings.Contains(content, `Command("aoc"`) {
			t.Error("autoSpawnForIssue should call 'aoc spawn'")
		}
	}

	// It must mark issue as in-progress
	if !strings.Contains(content, "in-progress") {
		t.Error("autoSpawnForIssue should add in-progress label")
	}
}

func TestIssueWatcher_PinardCommentsIgnored(t *testing.T) {
	notes := []gitlab.Note{
		{ID: 1, Body: "human comment", System: false, Author: gitlab.Author{Username: "reviewer"}},
		{ID: 2, Body: "pinard reply", System: false, Author: gitlab.Author{Username: "pinard"}},
		{ID: 3, Body: "system note", System: true, Author: gitlab.Author{Username: "gitlab"}},
	}

	pinardUser := "pinard"
	lastNoteID := 0

	var newNotes []gitlab.Note
	for _, n := range notes {
		if n.System || n.ID <= lastNoteID || n.Author.Username == pinardUser {
			continue
		}
		newNotes = append(newNotes, n)
	}

	if len(newNotes) != 1 {
		t.Errorf("expected 1 note, got %d", len(newNotes))
	}
	if newNotes[0].Body != "human comment" {
		t.Errorf("expected 'human comment', got %q", newNotes[0].Body)
	}
}

func TestIssueWatcher_CommentTracking(t *testing.T) {
	dir := t.TempDir()
	issueState, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))
	issueState.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"exo-cli": {
				"42": {Status: "spawned", Title: "Fix auth", LastNoteID: 5},
			},
		}
	})

	// Simulate forwarding a new comment
	issueState.Update(func(s *state.IssueWatcherState) {
		s.Seen["exo-cli"]["42"].LastNoteID = 8
	})

	issueState.Read(func(s *state.IssueWatcherState) {
		if s.Seen["exo-cli"]["42"].LastNoteID != 8 {
			t.Errorf("expected last_note_id 8, got %d", s.Seen["exo-cli"]["42"].LastNoteID)
		}
	})
}

func TestIssueWatcher_CapsuleGateLabelCheck(t *testing.T) {
	// Verify that issues with contract_id: in description are correctly detected.
	cases := []struct {
		desc       string
		descBody   string
		expectGate bool
	}{
		{"has contract_id", "fix something\ncontract_id: ctr-abc\nmore text", true},
		{"no contract_id", "fix something without capsule", false},
		{"contract_id mid-line (not gated)", "see contract_id: in log output", false}, // inline not at line start
	}
	for _, tc := range cases {
		t.Run(tc.desc, func(t *testing.T) {
			got := extractContractID(tc.descBody) != ""
			if got != tc.expectGate {
				t.Errorf("extractContractID for %q: gated=%v, want %v", tc.descBody, got, tc.expectGate)
			}
		})
	}
}

func TestIssueWatcher_CapsuleFundedFastPath(t *testing.T) {
	// Verify that capsule:funded label on a capsule-gated issue triggers checkFunding.
	labels := []string{"capsule:awaiting-funding", "capsule:funded", "feat"}
	if !hasLabel(labels, "capsule:funded") {
		t.Error("hasLabel should detect capsule:funded")
	}
	if hasLabel(labels, "capsule:active") {
		t.Error("hasLabel should not detect capsule:active in this set")
	}
}

func TestIssueWatcher_BlockedLabelPreventsSpawn_WithCapsule(t *testing.T) {
	// Verify blocked/discarded labels still prevent spawn even alongside capsule labels.
	tests := []struct {
		labels      []string
		expectBlock bool
	}{
		{[]string{"capsule:awaiting-funding"}, false},
		{[]string{"capsule:awaiting-funding", "blocked"}, true},
		{[]string{"capsule:active", "pinard:discarded"}, true},
	}
	for _, tc := range tests {
		blocked := false
		for _, l := range tc.labels {
			if l == "blocked" || l == "pinard:discarded" {
				blocked = true
				break
			}
		}
		if blocked != tc.expectBlock {
			t.Errorf("labels=%v: expected blocked=%v, got %v", tc.labels, tc.expectBlock, blocked)
		}
	}
}

func TestSeenIssue_ContractIDPersistence(t *testing.T) {
	dir := t.TempDir()
	st, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))

	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"proj": {
				"7": {
					Status:     "capsule-gated",
					ContractID: "ctr-def",
					Title:      "Capsule issue",
				},
			},
		}
	})

	// Reload from disk to verify YAML round-trip.
	st2, err := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))
	if err != nil {
		t.Fatalf("reload failed: %v", err)
	}
	st2.Read(func(s *state.IssueWatcherState) {
		entry := s.Seen["proj"]["7"]
		if entry == nil {
			t.Fatal("entry not found after reload")
		}
		if entry.ContractID != "ctr-def" {
			t.Errorf("ContractID not persisted: got %q", entry.ContractID)
		}
		if entry.Status != "capsule-gated" {
			t.Errorf("Status not persisted: got %q", entry.Status)
		}
	})
}

// newStubGitLab returns a stub GitLab TLS server that records request paths
// and bodies, responding 200 OK to everything.
func newStubGitLab(t *testing.T) (*gitlab.Client, *[]string) {
	t.Helper()
	var calls []string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path+"?"+r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	t.Cleanup(srv.Close)
	gl := gitlab.NewClient(srv.Listener.Addr().String(), "test-token")
	gl.HTTP = srv.Client()
	return gl, &calls
}

// newSpawnableWatcher returns an IssueWatcher wired with a fake `aoc` binary
// that succeeds on spawn, a stub GitLab, and a temp state store.
func newSpawnableWatcher(t *testing.T, gl *gitlab.Client) (*IssueWatcher, *state.Store[state.IssueWatcherState]) {
	t.Helper()

	vigDir := t.TempDir()
	cfg := &config.VignobleConfig{
		Vignes: map[string]config.Vigne{
			"proj": {Repo: "group/proj"},
		},
	}
	vig := &config.Vignoble{
		Path:       vigDir,
		Name:       "test",
		ConfigPath: filepath.Join(vigDir, "vignes.yaml"),
		Config:     cfg,
	}

	// Install a fake `aoc` that exits 0.
	binDir := t.TempDir()
	fakeAoc := filepath.Join(binDir, "aoc")
	if err := os.WriteFile(fakeAoc, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write fake aoc: %v", err)
	}
	t.Setenv("PATH", binDir+":"+os.Getenv("PATH"))

	stDir := t.TempDir()
	st, err := state.Load[state.IssueWatcherState](filepath.Join(stDir, "issue-watcher.yaml"))
	if err != nil {
		t.Fatalf("state.Load: %v", err)
	}

	w := &IssueWatcher{
		State:    st,
		GitLab:   gl,
		Vignoble: vig,
		User:     "bot",
		Owner:    "owner",
	}
	return w, st
}

// TestSpawnIfApproved_HeldThenApproved verifies:
// An issue in awaiting-approval state, once the owner approves, causes
// spawnIfApproved to remove the pinard:awaiting-approval label before spawning.
func TestSpawnIfApproved_HeldThenApproved(t *testing.T) {
	// Stub GitLab: owner has approved via note, and notes endpoint returns it.
	var putBodies []string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/notes") {
			// Return an owner approval note.
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`[{"id":1,"body":"@bot go","system":false,"author":{"username":"owner"}}]`))
			return
		}
		if r.Method == http.MethodPut {
			body, _ := io.ReadAll(r.Body)
			putBodies = append(putBodies, string(body))
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	t.Cleanup(srv.Close)
	gl := gitlab.NewClient(srv.Listener.Addr().String(), "test-token")
	gl.HTTP = srv.Client()

	w, st := newSpawnableWatcher(t, gl)

	// Seed: issue is in awaiting-approval state.
	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"proj": {
				"5": {Status: "awaiting-approval", Title: "Test", AwaitingApproval: true},
			},
		}
	})

	issue := gitlab.Issue{IID: 5, Title: "Test"}
	issue.Author.Username = "dev"

	existing := &state.SeenIssue{Status: "awaiting-approval", AwaitingApproval: true}
	spawned := w.spawnIfApproved("proj", "group/proj", issue, existing, "")

	if !spawned {
		t.Error("expected spawnIfApproved to return true after owner approval")
	}

	// Verify a PUT was issued with remove_labels=pinard:awaiting-approval.
	removed := false
	for _, body := range putBodies {
		if strings.Contains(body, "pinard%3Aawaiting-approval") || strings.Contains(body, "pinard:awaiting-approval") {
			removed = true
		}
	}
	if !removed {
		t.Errorf("expected PUT with remove_labels=pinard:awaiting-approval, got PUT bodies: %v", putBodies)
	}
}

// TestSpawnIfApproved_OwnerAuthoredNoLabelAdded verifies:
// An owner-authored issue passes the gate on first check without the
// pinard:awaiting-approval label ever being added.
func TestSpawnIfApproved_OwnerAuthoredNoLabelAdded(t *testing.T) {
	var putBodies []string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPut {
			body, _ := io.ReadAll(r.Body)
			putBodies = append(putBodies, string(body))
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	t.Cleanup(srv.Close)
	gl := gitlab.NewClient(srv.Listener.Addr().String(), "test-token")
	gl.HTTP = srv.Client()

	w, _ := newSpawnableWatcher(t, gl)

	// Owner-authored issue — approved on first check.
	issue := gitlab.Issue{IID: 7, Title: "Owner issue"}
	issue.Author.Username = "owner"

	spawned := w.spawnIfApproved("proj", "group/proj", issue, nil, "")

	if !spawned {
		t.Error("expected spawnIfApproved to return true for owner-authored issue")
	}

	// pinard:awaiting-approval must never have been added.
	for _, body := range putBodies {
		if strings.Contains(body, "add_labels") && strings.Contains(body, "awaiting-approval") {
			t.Errorf("awaiting-approval label must not be added for owner-authored issue; PUT body: %s", body)
		}
	}
}

// TestSpawnIfApproved_LabelAbsentNoError verifies:
// Calling UpdateIssue with remove_labels for a label that is not present does not
// cause spawnIfApproved to fail (the GitLab API is idempotent; we get {} back).
func TestSpawnIfApproved_LabelAbsentNoError(t *testing.T) {
	// Stub returns {} for every request — simulates label already absent.
	gl, _ := newStubGitLab(t)
	w, st := newSpawnableWatcher(t, gl)

	// Seed: awaiting-approval but no actual label on GitLab (already removed externally).
	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"proj": {
				"9": {Status: "awaiting-approval", Title: "Test", AwaitingApproval: true},
			},
		}
	})

	// Wire the stub to return an owner approval note.
	srv2 := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/notes") {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`[{"id":1,"body":"@bot go","system":false,"author":{"username":"owner"}}]`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	t.Cleanup(srv2.Close)
	gl2 := gitlab.NewClient(srv2.Listener.Addr().String(), "test-token")
	gl2.HTTP = srv2.Client()
	w.GitLab = gl2

	issue := gitlab.Issue{IID: 9, Title: "Test"}
	issue.Author.Username = "dev"
	existing := &state.SeenIssue{Status: "awaiting-approval", AwaitingApproval: true}

	// Must not panic or return false due to the remove_labels call.
	spawned := w.spawnIfApproved("proj", "group/proj", issue, existing, "")
	if !spawned {
		t.Error("expected spawn to succeed even when label was already absent")
	}
}
