package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGithubVignesYAML(t *testing.T) {
	tests := []struct {
		name     string
		host     string
		group    string
		wantKeys []string
	}{
		{
			name:     "default github.com host omitted",
			host:     "github.com",
			group:    "",
			wantKeys: []string{"pressoir:", "provider: github"},
		},
		{
			name:     "custom GHES host included",
			host:     "github.example.com",
			group:    "",
			wantKeys: []string{"pressoir:", "provider: github", "host: github.example.com"},
		},
		{
			name:     "org included",
			host:     "github.com",
			group:    "myorg",
			wantKeys: []string{"org: myorg"},
		},
		{
			name:     "vignes block present",
			host:     "",
			group:    "",
			wantKeys: []string{"vignes: {}"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := githubVignesYAML(tt.host, tt.group)
			for _, want := range tt.wantKeys {
				if !strings.Contains(got, want) {
					t.Errorf("githubVignesYAML(%q, %q): missing %q in output:\n%s", tt.host, tt.group, want, got)
				}
			}
		})
	}
}

func TestWriteServerCredentialsTemplate(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	credsPath := filepath.Join(home, ".config", "pinard", "credentials.yaml")

	// Should create the file when missing.
	if err := writeServerCredentialsTemplate("github", "github.com", "myorg"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	data, err := os.ReadFile(credsPath)
	if err != nil {
		t.Fatalf("credentials.yaml not created: %v", err)
	}
	content := string(data)
	if !strings.Contains(content, "github:") {
		t.Errorf("expected 'github:' block in template, got:\n%s", content)
	}
	if !strings.Contains(content, "PINARD_GITHUB_TOKEN") {
		t.Errorf("expected token_env hint in template")
	}

	// Should NOT overwrite an existing file.
	sentinel := "# existing credentials\n"
	if err := os.WriteFile(credsPath, []byte(sentinel), 0600); err != nil {
		t.Fatal(err)
	}
	if err := writeServerCredentialsTemplate("github", "github.com", ""); err != nil {
		t.Fatalf("unexpected error on second call: %v", err)
	}
	data2, _ := os.ReadFile(credsPath)
	if string(data2) != sentinel {
		t.Errorf("existing credentials.yaml was overwritten")
	}
}

func TestWriteServerCredentialsTemplateGitLab(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	if err := writeServerCredentialsTemplate("gitlab", "gitlab.example.com", ""); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	credsPath := filepath.Join(home, ".config", "pinard", "credentials.yaml")
	data, err := os.ReadFile(credsPath)
	if err != nil {
		t.Fatalf("credentials.yaml not created: %v", err)
	}
	content := string(data)
	if !strings.Contains(content, "gitlab:") {
		t.Errorf("expected 'gitlab:' block in template, got:\n%s", content)
	}
	if !strings.Contains(content, "PINARD_GITLAB_TOKEN") {
		t.Errorf("expected token_env hint in template")
	}
}

func TestAppendVigneToYAML(t *testing.T) {
	dir := t.TempDir()
	vignesPath := filepath.Join(dir, "vignes.yaml")

	initial := "vignes: {}\n"
	if err := os.WriteFile(vignesPath, []byte(initial), 0644); err != nil {
		t.Fatal(err)
	}

	v := &wizardVigne{name: "my-api", repo: "owner/my-api", autoMerge: true}
	appendVigneToYAML(vignesPath, v)

	data, err := os.ReadFile(vignesPath)
	if err != nil {
		t.Fatal(err)
	}
	content := string(data)
	if !strings.Contains(content, "my-api") {
		t.Errorf("expected vigne name in output:\n%s", content)
	}
	if !strings.Contains(content, "owner/my-api") {
		t.Errorf("expected repo in output:\n%s", content)
	}
	if !strings.Contains(content, "auto_merge") {
		t.Errorf("expected auto_merge in output:\n%s", content)
	}
}

func TestPromptTTYDefault(t *testing.T) {
	// promptTTY is only called on a live TTY; here we just verify it compiles
	// and returns the default when stdin is a non-TTY pipe (as in tests).
	// We redirect stdin to /dev/null so ReadString sees EOF immediately.
	old := os.Stdin
	f, err := os.Open(os.DevNull)
	if err != nil {
		t.Skip("cannot open /dev/null:", err)
	}
	os.Stdin = f
	defer func() {
		os.Stdin = old
		f.Close()
	}()

	got := promptTTY("test prompt", "mydefault")
	if got != "mydefault" {
		t.Errorf("expected default %q, got %q", "mydefault", got)
	}
}
