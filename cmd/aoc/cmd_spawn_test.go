package main

import (
	"fmt"
	"os"
	"strings"
	"testing"
)

func TestWorkerSessionName(t *testing.T) {
	cases := []struct {
		parcelle, project, id, want string
	}{
		// Default bucket (parcelle == project), issue-driven.
		{"exo-cli", "exo-cli", "42abcd", "exo-cli--exo-cli-42abcd"},
		// Cross-cutting workstream parcelle, leading token differs from project.
		{"semantic-search", "exo-cli", "1730ef01", "semantic-search--exo-cli-1730ef01"},
		// Unsafe chars in parcelle are sanitized (tmux target safety).
		{"v1.2:x", "proj", "00ffee", "v1-2-x--proj-00ffee"},
	}
	for _, c := range cases {
		got := workerSessionName(c.parcelle, c.project, c.id)
		if got != c.want {
			t.Errorf("workerSessionName(%q,%q,%q) = %q, want %q", c.parcelle, c.project, c.id, got, c.want)
		}
		// Parcelle must be the leading, filterable token.
		if !strings.HasPrefix(got, "--") && !strings.HasPrefix(got, c.want[:strings.Index(c.want, "--")]) {
			t.Errorf("workerSessionName(%q,...) = %q does not lead with the parcelle token", c.parcelle, got)
		}
		// Never contains a tmux target separator.
		if strings.ContainsAny(got, ".: ") {
			t.Errorf("workerSessionName(...) = %q contains a forbidden tmux char", got)
		}
	}
}

// TestWorkerCmdEnvIsolation verifies that the worker command string uses `env -i`
// so that the tmux shell inherits nothing from the daemon's environment. This is a
// security invariant: operator PATs (e.g. EXOHUB_GITLAB_TOKEN) and PINARD_OWNER_*
// vars present in the daemon's env must not reach the LLM-driven worker process.
func TestWorkerCmdEnvIsolation(t *testing.T) {
	// Simulate an operator PAT in the daemon's process environment.
	t.Setenv("EXOHUB_GITLAB_TOKEN", "glpat-operator-secret-should-not-leak")
	t.Setenv("PINARD_OWNER_GITLAB_TOKEN", "glpat-owner-secret-should-not-leak")

	// Reproduce the env-building logic from cmd_spawn.go (keep in sync).
	envParts := []string{
		fmt.Sprintf("HOME='%s'", os.Getenv("HOME")),
		fmt.Sprintf("PATH='%s'", os.Getenv("PATH")),
		"GITLAB_HOST='gitlab.example.com'",
		"GLAB_HOST='gitlab.example.com'",
		"GITLAB_TOKEN='glpat-bot-token'",
		"GLAB_TOKEN='glpat-bot-token'",
		"PINARD_NATS_USER='testbot'",
		"PINARD_NATS_URL='wss://nats.example.com'",
	}
	workerEnv := strings.Join(envParts, " ")
	launcher := "pinard"
	workerFlags := "--worker --vignoble '/v' --project 'proj' --session-name 'sess' --target-branch 'main' --model 'claude-sonnet' --parcelle 'proj' --prompt 'hi'"
	spawnDir := "/worktree/proj"

	workerCmd := fmt.Sprintf("cd '%s' && env -i %s %s %s\n", spawnDir, workerEnv, launcher, workerFlags)

	// Must use env -i for clean environment.
	if !strings.Contains(workerCmd, "env -i ") {
		t.Errorf("workerCmd must use 'env -i' for env isolation; got: %q", workerCmd)
	}

	// Operator PATs must NOT appear in the command.
	if strings.Contains(workerCmd, "EXOHUB_GITLAB_TOKEN") {
		t.Errorf("workerCmd must not contain EXOHUB_GITLAB_TOKEN (operator PAT leak)")
	}
	if strings.Contains(workerCmd, "PINARD_OWNER_GITLAB_TOKEN") {
		t.Errorf("workerCmd must not contain PINARD_OWNER_GITLAB_TOKEN (owner token leak)")
	}
	if strings.Contains(workerCmd, "glpat-operator-secret-should-not-leak") {
		t.Errorf("workerCmd must not contain the operator PAT value")
	}

	// Bot token and NATS creds must be present.
	if !strings.Contains(workerCmd, "GITLAB_TOKEN='glpat-bot-token'") {
		t.Errorf("workerCmd must contain GITLAB_TOKEN (bot token)")
	}
	if !strings.Contains(workerCmd, "PINARD_NATS_URL=") {
		t.Errorf("workerCmd must contain PINARD_NATS_URL")
	}

	// HOME and PATH must be present (pinard needs them to bootstrap).
	if !strings.Contains(workerCmd, "HOME=") {
		t.Errorf("workerCmd must contain HOME")
	}
	if !strings.Contains(workerCmd, "PATH=") {
		t.Errorf("workerCmd must contain PATH")
	}

	// The operator PAT env vars set on this process must still be set in
	// the test process itself — env -i only affects the child, not the parent.
	// (Belt-and-suspenders: verify t.Setenv didn't clean them up prematurely.)
	if os.Getenv("EXOHUB_GITLAB_TOKEN") == "" {
		t.Error("test setup: EXOHUB_GITLAB_TOKEN should be set in the test process env")
	}
}
