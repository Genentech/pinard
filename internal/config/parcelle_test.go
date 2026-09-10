package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeParcelleYAML(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "parcelle.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadParcelleConfig_TargetBranchInlineComment(t *testing.T) {
	path := writeParcelleYAML(t, `name: comms
status: active
target_branch: master  # Phases 1–3 landed; follow-ups target master directly
issues:
  - 243
  - 244
`)
	cfg, err := LoadParcelleConfig(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.TargetBranch != "master" {
		t.Errorf("expected target_branch %q, got %q", "master", cfg.TargetBranch)
	}
}

func TestLoadParcelleConfig_TargetBranchQuoted(t *testing.T) {
	path := writeParcelleYAML(t, `name: cuvee-work
status: active
target_branch: "cuvee/my-feature"
issues:
  - 100
`)
	cfg, err := LoadParcelleConfig(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.TargetBranch != "cuvee/my-feature" {
		t.Errorf("expected target_branch %q, got %q", "cuvee/my-feature", cfg.TargetBranch)
	}
}

func TestLoadParcelleConfig_NoTargetBranch(t *testing.T) {
	path := writeParcelleYAML(t, `name: simple
status: active
issues:
  - 10
`)
	cfg, err := LoadParcelleConfig(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.TargetBranch != "" {
		t.Errorf("expected empty target_branch, got %q", cfg.TargetBranch)
	}
}

func TestLoadParcelleConfig_Issues(t *testing.T) {
	path := writeParcelleYAML(t, `name: multi
status: active
issues:
  - 1   # done
  - 2
  - 42
`)
	cfg, err := LoadParcelleConfig(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []int{1, 2, 42}
	if len(cfg.Issues) != len(want) {
		t.Fatalf("expected %d issues, got %d: %v", len(want), len(cfg.Issues), cfg.Issues)
	}
	for i, v := range want {
		if cfg.Issues[i] != v {
			t.Errorf("issues[%d]: expected %d, got %d", i, v, cfg.Issues[i])
		}
	}
}

func TestLoadParcelleConfig_StatusArchived(t *testing.T) {
	path := writeParcelleYAML(t, `name: old
status: archived
target_branch: master
`)
	cfg, err := LoadParcelleConfig(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Status != "archived" {
		t.Errorf("expected status %q, got %q", "archived", cfg.Status)
	}
}

func TestLoadParcelleConfig_FileNotFound(t *testing.T) {
	_, err := LoadParcelleConfig("/nonexistent/parcelle.yaml")
	if err == nil {
		t.Error("expected error for missing file, got nil")
	}
}
