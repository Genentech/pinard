package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestValidatePath(t *testing.T) {
	valid := []string{
		"models.conductor.id",
		"models.worker.id",
		"auto_merge",
		"gitlab_host",
		"gitlab_group",
		"vignes.charon.model.id",
		"vignes.charon.auto_merge",
		"vignes.charon.monitor_post_merge",
		"vignes.charon.path",
		"vignes.charon.repo",
		"vignes.exo-cli.model.id",
	}
	for _, p := range valid {
		if err := validatePath(p); err != nil {
			t.Errorf("validatePath(%q) should be valid, got: %v", p, err)
		}
	}

	invalid := []string{
		"",
		"invalid",
		"models",
		"models.conductor",
		"models.conductor.oops",
		"models.admin.id",
		"vignes",
		"vignes.charon",
		"vignes.charon.badfield",
		"vignes.charon.model",
		"vignes.charon.model.oops",
		"vignes.charon.auto_merge.extra",
		"auto_merge.extra",
		"foo.bar.baz",
	}
	for _, p := range invalid {
		if err := validatePath(p); err == nil {
			t.Errorf("validatePath(%q) should be invalid, but passed", p)
		}
	}
}

func TestConfigSetGet(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "vignes.yaml")
	os.WriteFile(cfg, []byte(`gitlab_host: gitlab.example.com
vignes:
  charon:
    path: ~/charon
    repo: GP/charon
`), 0644)

	os.Setenv("AOC_CONFIG", cfg)
	defer os.Unsetenv("AOC_CONFIG")

	// Set models.worker.id
	if err := configSet("models.worker.id", "claude-sonnet-4-6"); err != nil {
		t.Fatalf("configSet models.worker.id: %v", err)
	}
	val, err := configGet("models.worker.id")
	if err != nil {
		t.Fatalf("configGet models.worker.id: %v", err)
	}
	if val != "claude-sonnet-4-6" {
		t.Errorf("expected claude-sonnet-4-6, got %q", val)
	}

	// Set models.conductor.id
	if err := configSet("models.conductor.id", "claude-opus-4-6"); err != nil {
		t.Fatalf("configSet models.conductor.id: %v", err)
	}
	val, _ = configGet("models.conductor.id")
	if val != "claude-opus-4-6" {
		t.Errorf("expected claude-opus-4-6, got %q", val)
	}

	// Set vignes.charon.model.id
	if err := configSet("vignes.charon.model.id", "claude-opus-4-6"); err != nil {
		t.Fatalf("configSet vignes.charon.model.id: %v", err)
	}
	val, _ = configGet("vignes.charon.model.id")
	if val != "claude-opus-4-6" {
		t.Errorf("expected claude-opus-4-6, got %q", val)
	}

	// Get existing value
	val, _ = configGet("vignes.charon.repo")
	if val != "GP/charon" {
		t.Errorf("expected GP/charon, got %q", val)
	}

	// Set auto_merge (boolean)
	if err := configSet("auto_merge", "true"); err != nil {
		t.Fatalf("configSet auto_merge: %v", err)
	}
	val, _ = configGet("auto_merge")
	if val != "true" {
		t.Errorf("expected true, got %q", val)
	}

	// Invalid path rejected
	if err := configSet("invalid.path", "foo"); err == nil {
		t.Error("expected error for invalid path")
	}
}

func TestConfigGetNotSet(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "vignes.yaml")
	os.WriteFile(cfg, []byte("gitlab_host: gitlab.example.com\nvignes: {}\n"), 0644)

	os.Setenv("AOC_CONFIG", cfg)
	defer os.Unsetenv("AOC_CONFIG")

	_, err := configGet("models.worker.id")
	if err == nil {
		t.Error("expected error when path not set")
	}
}
