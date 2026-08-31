package wiki

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEnsureBundle_AbsentDir(t *testing.T) {
	dir := t.TempDir()

	if err := EnsureBundle(dir); err != nil {
		t.Fatalf("EnsureBundle: %v", err)
	}

	wikiDir := filepath.Join(dir, "wiki")
	requiredFiles := []string{"index.md", "INSTRUCTIONS.md", "log.md"}
	for _, f := range requiredFiles {
		p := filepath.Join(wikiDir, f)
		if _, err := os.Stat(p); err != nil {
			t.Errorf("expected file %s to exist: %v", f, err)
		}
	}

	// index.md must contain okf_version: "0.1"
	data, err := os.ReadFile(filepath.Join(wikiDir, "index.md"))
	if err != nil {
		t.Fatalf("read index.md: %v", err)
	}
	if !strings.Contains(string(data), `okf_version: "0.1"`) {
		t.Errorf("index.md missing okf_version: \"0.1\"; got:\n%s", string(data))
	}
}

func TestEnsureBundle_Idempotent(t *testing.T) {
	dir := t.TempDir()

	if err := EnsureBundle(dir); err != nil {
		t.Fatalf("first EnsureBundle: %v", err)
	}

	indexPath := filepath.Join(dir, "wiki", "index.md")
	fi1, err := os.Stat(indexPath)
	if err != nil {
		t.Fatalf("stat after first call: %v", err)
	}
	mtime1 := fi1.ModTime()

	// Ensure mtime resolution doesn't mask a rewrite on fast filesystems.
	time.Sleep(10 * time.Millisecond)

	if err := EnsureBundle(dir); err != nil {
		t.Fatalf("second EnsureBundle: %v", err)
	}

	fi2, err := os.Stat(indexPath)
	if err != nil {
		t.Fatalf("stat after second call: %v", err)
	}
	if !fi2.ModTime().Equal(mtime1) {
		t.Errorf("index.md was rewritten on second call (mtime changed)")
	}
}

func TestEnsureBundle_WhenWikiDirExists(t *testing.T) {
	dir := t.TempDir()

	// Pre-create wiki/ with an index.md (simulates already-seeded state).
	wikiDir := filepath.Join(dir, "wiki")
	if err := os.MkdirAll(wikiDir, 0755); err != nil {
		t.Fatal(err)
	}
	existing := []byte("# existing content\n")
	if err := os.WriteFile(filepath.Join(wikiDir, "index.md"), existing, 0644); err != nil {
		t.Fatal(err)
	}

	if err := EnsureBundle(dir); err != nil {
		t.Fatalf("EnsureBundle with existing index: %v", err)
	}

	// Content must not be overwritten.
	data, err := os.ReadFile(filepath.Join(wikiDir, "index.md"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(existing) {
		t.Errorf("existing index.md was overwritten; got:\n%s", string(data))
	}
}
