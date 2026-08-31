//go:build !capsule

package watcher

import (
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/state"
)

// CapsulePoller is a no-op stub when the capsule build tag is absent.
// The struct mirrors the real CapsulePoller's exported fields so daemon.go
// can construct it identically without build-tag guards at the call site.
type CapsulePoller struct {
	State        *state.Store[state.IssueWatcherState]
	GitLab       *gitlab.Client
	Vignoble     *config.Vignoble
	User         string
	Owner        string
	issueWatcher *IssueWatcher
}

func (p *CapsulePoller) SetIssueWatcher(w *IssueWatcher) {}
func (p *CapsulePoller) Run()                             {}
func (p *CapsulePoller) checkFunding(vigneName, repo string, issue gitlab.Issue, contractID string) {
}

// CapsulePollInterval returns the slow-poll duration (stub: returns default 20m).
func CapsulePollInterval() time.Duration { return 20 * time.Minute }

// ContractResult is the output of resolveIssueContract.
// Stub: always returns zero value (no contract found) in non-capsule builds.
type ContractResult struct {
	ContractID  string
	Funded      bool
	PubkeyMatch bool
	Transient   bool
	Error       string
}

// resolveIssueContract always returns an empty ContractResult in non-capsule
// builds — no contract gating without the capsule feature.
func resolveIssueContract(_ *gitlab.Client, _ string, _ int, _ string) ContractResult {
	return ContractResult{}
}

// ResolveIssueContract is the exported variant (used by cmd/aoc/cmd_spawn.go).
func ResolveIssueContract(_ *gitlab.Client, _ string, _ int, _ string) ContractResult {
	return ContractResult{}
}

// mnemosyneURL returns "" in non-capsule builds (used by capsule_orphan_stub.go
// indirectly; defined here to satisfy any shared references).
func mnemosyneURL() string { return "" }
