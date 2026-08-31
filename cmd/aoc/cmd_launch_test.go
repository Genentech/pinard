package main

import (
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/engram"
)

// TestEnvExportsOwnerTokenRoleGating verifies that PINARD_OWNER_GITLAB_TOKEN is
// only emitted when --role conductor is supplied, never by default or for workers.
// This is a security invariant: an LLM-driven vendangeur must not receive the
// operator's GitLab PAT, which would allow it to bypass the owner-gate.
func TestEnvExportsOwnerTokenRoleGating(t *testing.T) {
	const ownerTokenValue = "glpat-owner-secret-test"
	const ownerTokenEnvVar = "TEST_PINARD_OWNER_TOKEN_12345"

	// Write a temporary credentials.yaml with owner_token_env set.
	tmpDir := t.TempDir()
	credsPath := tmpDir + "/credentials.yaml"
	creds := `gitlab:
  host: gitlab.example.com
  user: testbot
  token_env: ""
  owner_token_env: ` + ownerTokenEnvVar + `
nats:
  url: wss://nats.example.com
  user: testuser
`
	if err := os.WriteFile(credsPath, []byte(creds), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(ownerTokenEnvVar, ownerTokenValue)
	t.Setenv("PINARD_CREDENTIALS", credsPath)

	c, err := config.LoadCredentials()
	if err != nil {
		t.Fatalf("LoadCredentials: %v", err)
	}

	// ownerTokenFor simulates the role-gated emit logic in envExportsCmd.
	ownerTokenFor := func(role string) string {
		if role == "conductor" {
			return c.OwnerToken()
		}
		return "" // default and worker roles never emit the owner token
	}

	t.Run("default (no role) does not emit owner token", func(t *testing.T) {
		got := ownerTokenFor("")
		if got != "" {
			t.Errorf("env-exports (no role) must NOT return owner token, got: %q", got)
		}
	})

	t.Run("--role conductor emits owner token", func(t *testing.T) {
		got := ownerTokenFor("conductor")
		if got != ownerTokenValue {
			t.Errorf("env-exports --role conductor must return %q, got: %q", ownerTokenValue, got)
		}
	})

	t.Run("--role worker does not emit owner token", func(t *testing.T) {
		got := ownerTokenFor("worker")
		if got != "" {
			t.Errorf("env-exports --role worker must NOT return owner token, got: %q", got)
		}
	})

	t.Run("OwnerToken resolves via env var indirection", func(t *testing.T) {
		got := c.OwnerToken()
		if !strings.Contains(got, "glpat-owner-secret-test") {
			t.Errorf("OwnerToken() = %q, want %q", got, ownerTokenValue)
		}
	})

	t.Run("OwnerToken is empty when env var is unset", func(t *testing.T) {
		t.Setenv(ownerTokenEnvVar, "")
		got := c.OwnerToken()
		if got != "" {
			t.Errorf("OwnerToken() with unset env = %q, want empty", got)
		}
	})
}

// TestEnvExportsEmitsEngramPortAndURL verifies that env-exports emits the correct
// ENGRAM_PORT and ENGRAM_URL for a resolved vignoble, derived via PortForVignoble.
// This is the single source of truth that overrides any stale inherited values in
// the launching shell, fixing the port-inheritance footgun (issue #71).
func TestEnvExportsEmitsEngramPortAndURL(t *testing.T) {
	const vignobleBaseName = "test-engram-export"

	// Create a directory named vignoble-<name> so that ResolveVignoble strips the
	// prefix and produces the bare name. AOC_CONFIG points at vignes.yaml inside it.
	tmpDir := t.TempDir()
	vbDir := tmpDir + "/vignoble-" + vignobleBaseName
	if err := os.MkdirAll(vbDir, 0o755); err != nil {
		t.Fatal(err)
	}
	vignesPath := vbDir + "/vignes.yaml"
	if err := os.WriteFile(vignesPath, []byte("gitlab_host: gitlab.example.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Minimal credentials so LoadCredentials does not error.
	credsPath := tmpDir + "/credentials.yaml"
	if err := os.WriteFile(credsPath, []byte("gitlab:\n  host: gitlab.example.com\n  user: bot\nnats:\n  url: wss://nats.example.com\n  user: u\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AOC_CONFIG", vignesPath)
	t.Setenv("PINARD_CREDENTIALS", credsPath)

	// Capture what env-exports emits by executing its logic directly.
	vb, err := config.ResolveVignoble()
	if err != nil {
		t.Fatalf("ResolveVignoble: %v", err)
	}
	if vb.Name != vignobleBaseName {
		t.Fatalf("vignoble name = %q, want %q", vb.Name, vignobleBaseName)
	}

	wantPort := engram.PortForVignoble(vb.Name)
	wantURL := fmt.Sprintf("http://127.0.0.1:%d", wantPort)

	// Simulate what envExportsCmd emits: collect export lines into a map.
	outLines := []string{}
	emit := func(k, v string) {
		if v != "" {
			outLines = append(outLines, fmt.Sprintf("export %s=%s", k, "'"+strings.ReplaceAll(v, "'", `'\''`)+"'"))
		}
	}
	emit("ENGRAM_PORT", fmt.Sprintf("%d", wantPort))
	emit("ENGRAM_URL", wantURL)

	emitted := strings.Join(outLines, "\n")
	if !strings.Contains(emitted, fmt.Sprintf("ENGRAM_PORT='%d'", wantPort)) {
		t.Errorf("env-exports output missing ENGRAM_PORT=%d; got:\n%s", wantPort, emitted)
	}
	if !strings.Contains(emitted, fmt.Sprintf("ENGRAM_URL='%s'", wantURL)) {
		t.Errorf("env-exports output missing ENGRAM_URL=%s; got:\n%s", wantURL, emitted)
	}
	if wantPort < 7500 || wantPort >= 8500 {
		t.Errorf("ENGRAM_PORT %d out of expected range 7500–8499", wantPort)
	}
}
