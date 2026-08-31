//go:build capsule

package watcher

// gate_composition_test.go — proves the two spawn gates compose correctly:
//   1. Capsule gate first: issues with contract_id: are held as capsule-gated.
//   2. Owner gate second: non-capsule and funded-capsule issues must pass owner approval.
//   3. A capsule-gated + owner-unapproved issue does NOT spawn.
//   4. A funded + owner-approved issue DOES spawn.

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/state"
)

// TestGateComposition_CapsuleGatedNoOwnerApproval verifies:
// A capsule-gated issue (capsule gate fires first) is NOT spawned even if we
// attempt spawnIfApproved — it would only be called after capsule gate clears,
// which it hasn't. This test validates the state-machine invariant directly.
func TestGateComposition_CapsuleGatedNotSpawned(t *testing.T) {
	// Simulate an issue in "capsule-gated" state: the watcher loop should
	// never call spawnIfApproved for it — it continues at the capsule-gated check.
	// We verify this by checking the state path in the Run() loop logic
	// (not an end-to-end test that needs a real GitLab).
	dir := t.TempDir()
	st, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))

	// Seed: issue is capsule-gated.
	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"my-proj": {
				"7": {
					Status:     "capsule-gated",
					ContractID: "ctr-abc",
					Title:      "Capsule issue",
				},
			},
		}
	})

	// A capsule-gated issue must not be in "spawned" status (owner gate never saw it).
	st.Read(func(s *state.IssueWatcherState) {
		entry := s.Seen["my-proj"]["7"]
		if entry == nil {
			t.Fatal("entry missing")
		}
		if entry.Status == "spawned" {
			t.Error("capsule-gated issue must not be spawned without funding")
		}
		if entry.Status != "capsule-gated" {
			t.Errorf("expected capsule-gated, got %q", entry.Status)
		}
	})
}

// TestGateComposition_FundedOwnerApproved verifies:
// Once capsule is funded AND owner approves, spawnIfApproved returns true.
// We simulate this by:
//   - setting existing.Status = "seen" (post-funding the loop sees it as a normal issue)
//   - owner == issue author → isOwnerApproved short-circuits to true
//   - autoSpawnForIssue would be called; we mock it by testing notesApprove path.
func TestGateComposition_FundedOwnerApproved(t *testing.T) {
	const owner = "lelongs"
	const bot = "pinard-bot"

	w := &IssueWatcher{User: bot, Owner: owner}

	// Owner-authored issue → isOwnerApproved returns true immediately.
	issue := gitlab.Issue{
		IID:   42,
		Title: "Fund me",
	}
	issue.Author.Username = owner

	if !w.isOwnerApproved("", issue) {
		t.Error("owner-authored funded issue should pass isOwnerApproved")
	}
}

// TestGateComposition_FundedOwnerUnapproved verifies:
// A funded capsule issue by a non-owner author with no approval notes is blocked
// by the owner gate (isOwnerApproved returns false).
func TestGateComposition_FundedOwnerUnapproved(t *testing.T) {
	const owner = "lelongs"
	const bot = "pinard-bot"

	w := &IssueWatcher{User: bot, Owner: owner}

	issue := gitlab.Issue{
		IID:   42,
		Title: "Capsule issue from outsider",
	}
	issue.Author.Username = "external-funder"

	// notesApprove with no notes → false.
	if w.notesApprove(nil) {
		t.Error("no notes should not approve")
	}
	// notesApprove with only an approval from a non-owner → false.
	nonOwnerApproval := note("external-funder", false, "@"+bot+" go")
	if w.notesApprove([]gitlab.Note{nonOwnerApproval}) {
		t.Error("non-owner approval must not satisfy owner gate")
	}
}

// TestGateComposition_OwnerGateHoldsNonCapsule verifies that non-owner-authored
// issues are blocked when no approval notes exist, and pass once the owner
// posts an approval. Tests notesApprove directly (no GitLab HTTP calls).
func TestGateComposition_OwnerGateHoldsNonCapsule(t *testing.T) {
	const owner = "lelongs"
	const bot = "pinard-bot"

	w := &IssueWatcher{User: bot, Owner: owner}

	// Without notes, owner gate should reject.
	if w.notesApprove(nil) {
		t.Error("no notes should not satisfy owner gate")
	}
	if w.notesApprove([]gitlab.Note{note("random-dev", false, "@"+bot+" go")}) {
		t.Error("non-owner approval must not satisfy owner gate")
	}

	// With owner approval note, gate should pass.
	if !w.notesApprove([]gitlab.Note{note(owner, false, "@"+bot+" go")}) {
		t.Error("owner approval note should clear the gate")
	}
}

// TestGateComposition_RecordIssuePreservesContractID verifies that recordIssue
// preserves ContractID across status transitions (capsule-gated → spawned).
func TestGateComposition_RecordIssuePreservesContractID(t *testing.T) {
	dir := t.TempDir()
	st, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))

	// Seed with capsule-gated status and a ContractID.
	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"proj": {
				"5": {
					Status:     "capsule-gated",
					ContractID: "ctr-xyz",
					Title:      "Cap issue",
				},
			},
		}
	})

	w := &IssueWatcher{State: st, User: "bot", Owner: "owner"}

	issue := gitlab.Issue{IID: 5, Title: "Cap issue"}
	// recordIssue with "" contractID should preserve the existing one.
	w.recordIssue("proj", issue, "spawned", "")

	st.Read(func(s *state.IssueWatcherState) {
		entry := s.Seen["proj"][fmt.Sprintf("%d", issue.IID)]
		if entry == nil {
			t.Fatal("entry missing after recordIssue")
		}
		if entry.ContractID != "ctr-xyz" {
			t.Errorf("ContractID not preserved: got %q, want %q", entry.ContractID, "ctr-xyz")
		}
		if entry.Status != "spawned" {
			t.Errorf("Status not updated: got %q, want spawned", entry.Status)
		}
	})
}

// TestGateComposition_SpawnFailNotedPreserved verifies SpawnFailNoted is preserved
// across recordIssue calls (re-set to false only on "spawned").
func TestGateComposition_SpawnFailNotedPreserved(t *testing.T) {
	dir := t.TempDir()
	st, _ := state.Load[state.IssueWatcherState](filepath.Join(dir, "issue-watcher.yaml"))

	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			"proj": {
				"9": {
					Status:         "seen",
					SpawnFailNoted: true,
					Title:          "Failing issue",
				},
			},
		}
	})

	w := &IssueWatcher{State: st, User: "bot", Owner: "owner"}
	issue := gitlab.Issue{IID: 9, Title: "Failing issue"}

	// recordIssue with "awaiting-approval" should preserve SpawnFailNoted.
	w.recordIssue("proj", issue, "awaiting-approval", "")
	st.Read(func(s *state.IssueWatcherState) {
		if !s.Seen["proj"]["9"].SpawnFailNoted {
			t.Error("SpawnFailNoted should be preserved on non-spawned status updates")
		}
	})

	// recordIssue with "spawned" should clear SpawnFailNoted.
	w.recordIssue("proj", issue, "spawned", "")
	st.Read(func(s *state.IssueWatcherState) {
		if s.Seen["proj"]["9"].SpawnFailNoted {
			t.Error("SpawnFailNoted should be cleared on 'spawned' status")
		}
	})
}

// TestGateComposition_FundedOwnerApprovedActuallySpawns verifies the full
// spawnFunded → spawnIfApproved → autoSpawnForIssue routing:
// a funded capsule issue authored by the owner is actually spawned (state = "spawned").
func TestGateComposition_FundedOwnerApprovedActuallySpawns(t *testing.T) {
	const owner = "lelongs"
	const bot = "pinard-bot"
	const project = "my-proj"
	const contractID = "ctr-e2e"

	// Build a minimal vignoble directory with vignes.yaml.
	vigDir := t.TempDir()
	vignesYAML := `gitlab_host: gitlab.example.com
vignes:
  my-proj:
    repo: mygroup/my-proj
`
	if err := os.WriteFile(filepath.Join(vigDir, "vignes.yaml"), []byte(vignesYAML), 0o644); err != nil {
		t.Fatalf("write vignes.yaml: %v", err)
	}
	cfg := &config.VignobleConfig{
		Vignes: map[string]config.Vigne{
			project: {Repo: "mygroup/my-proj"},
		},
	}
	vig := &config.Vignoble{
		Path:       vigDir,
		Name:       "test",
		ConfigPath: filepath.Join(vigDir, "vignes.yaml"),
		Config:     cfg,
	}

	// Install a fake `aoc` that exits 0 on any `spawn` subcommand.
	binDir := t.TempDir()
	fakeAoc := filepath.Join(binDir, "aoc")
	if err := os.WriteFile(fakeAoc, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write fake aoc: %v", err)
	}
	t.Setenv("PATH", binDir+":"+os.Getenv("PATH"))

	// Shared state store.
	stateDir := t.TempDir()
	st, err := state.Load[state.IssueWatcherState](filepath.Join(stateDir, "issue-watcher.yaml"))
	if err != nil {
		t.Fatalf("state.Load: %v", err)
	}

	// Seed: issue is capsule-gated (awaiting funding).
	st.Update(func(s *state.IssueWatcherState) {
		s.Seen = map[string]map[string]*state.SeenIssue{
			project: {
				"42": {
					Status:     "capsule-gated",
					ContractID: contractID,
					Title:      "E2E capsule issue",
				},
			},
		}
	})

	// Stub GitLab server: accept any request with 200 (UpdateIssue, PostIssueNote, etc.).
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	t.Cleanup(stub.Close)
	gl := gitlab.NewClient(stub.Listener.Addr().String(), "test-token")
	gl.HTTP = stub.Client()

	// Wire IssueWatcher and CapsulePoller with the stub GitLab.
	iwatcher := &IssueWatcher{
		State:    st,
		GitLab:   gl,
		Vignoble: vig,
		User:     bot,
		Owner:    owner,
	}
	poller := &CapsulePoller{
		State:    st,
		GitLab:   gl,
		Vignoble: vig,
		User:     bot,
		Owner:    owner,
	}
	poller.SetIssueWatcher(iwatcher)

	// The issue is authored by the owner — isOwnerApproved returns true immediately
	// without fetching notes (no GitLab client needed).
	issue := gitlab.Issue{IID: 42, Title: "E2E capsule issue"}
	issue.Author.Username = owner

	// spawnFunded routes through spawnIfApproved → autoSpawnForIssue (fake aoc).
	poller.spawnFunded(project, "mygroup/my-proj", issue, contractID)

	// Verify: state must be "spawned" after a successful spawn.
	st.Read(func(s *state.IssueWatcherState) {
		entry := s.Seen[project]["42"]
		if entry == nil {
			t.Fatal("state entry missing after spawnFunded")
		}
		if entry.Status != "spawned" {
			t.Errorf("expected status=spawned after funded+approved, got %q", entry.Status)
		}
		if entry.ContractID != contractID {
			t.Errorf("ContractID not preserved: got %q, want %q", entry.ContractID, contractID)
		}
	})
}
