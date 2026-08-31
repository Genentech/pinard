package watcher

import (
	"testing"

	"github.com/Genentech/pinard/internal/config"
)

func TestMentionsUser(t *testing.T) {
	w := &IssueWatcher{User: "pinard"}
	cases := []struct {
		body string
		want bool
	}{
		{"hey @pinard can you fix this", true},
		{"@pinard", true},
		{"cc @pinard, thanks", true},
		{"no mention here", false},
		{"email me at a@pinard.com", false}, // '.' after tag → not a mention
		{"talk to @pinardbot instead", false}, // longer username, not @pinard
		{"", false},
	}
	for _, c := range cases {
		if got := w.mentionsUser(c.body); got != c.want {
			t.Errorf("mentionsUser(%q) = %v, want %v", c.body, got, c.want)
		}
	}
	// Empty configured user never matches.
	if (&IssueWatcher{User: ""}).mentionsUser("@ hi") {
		t.Errorf("empty user should not match")
	}
}

func TestFindAgentForIssue(t *testing.T) {
	kv := newMockKV()
	kv.setAgent("exo-cli-swe-42", map[string]any{"project": "exo-cli", "process": "swe", "issue": "42"})
	kv.setAgent("exo-cli-swe-99", map[string]any{"project": "exo-cli", "process": "swe", "issue": "99"})
	kv.setAgent("other-swe-42", map[string]any{"project": "other", "process": "swe", "issue": "42"})

	w := &IssueWatcher{KV: kv, Vignoble: &config.Vignoble{Name: "exohub"}}

	if got := w.findAgentForIssue("exo-cli", 42); got != "exo-cli-swe-42" {
		t.Errorf("findAgentForIssue(exo-cli,42) = %q, want exo-cli-swe-42", got)
	}
	// Wrong project must not match another project's same IID.
	if got := w.findAgentForIssue("exo-cli", 7); got != "" {
		t.Errorf("findAgentForIssue(exo-cli,7) = %q, want \"\" (no guess)", got)
	}
}

func TestFindAgentForIssue_SkipsOtherVignoble(t *testing.T) {
	kv := newMockKV()
	// Same project + IID but belonging to a DIFFERENT vignoble (global bucket).
	kv.setAgent("exo-cli-swe-42", map[string]any{
		"project": "exo-cli", "process": "swe", "issue": "42", "vignoble": "exohub",
	})
	w := &IssueWatcher{KV: kv, Vignoble: &config.Vignoble{Name: "targetnexus"}}
	if got := w.findAgentForIssue("exo-cli", 42); got != "" {
		t.Errorf("cross-vignoble leak: got %q, want \"\" (entry belongs to exohub, watcher is targetnexus)", got)
	}
	// Same entry, matching vignoble → found.
	kv.setAgent("exo-cli-swe-42", map[string]any{
		"project": "exo-cli", "process": "swe", "issue": "42", "vignoble": "targetnexus",
	})
	if got := w.findAgentForIssue("exo-cli", 42); got != "exo-cli-swe-42" {
		t.Errorf("same-vignoble match failed: got %q", got)
	}
}

func TestIssueMatches(t *testing.T) {
	if !issueMatches("42", "42") {
		t.Error("string issue should match")
	}
	if !issueMatches(float64(42), "42") {
		t.Error("numeric issue should match")
	}
	if issueMatches("43", "42") {
		t.Error("mismatched issue should not match")
	}
	if issueMatches(nil, "42") {
		t.Error("nil issue should not match")
	}
}
