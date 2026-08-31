package wiki

import (
	"embed"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

//go:embed templates
var templates embed.FS

// EnsureBundle idempotently seeds a valid OKF v0.1 bundle under
// <vignoblePath>/wiki/. It is safe to call on every daemon start.
//
// On first creation it attempts a git commit inside vignoblePath so the
// scaffold is immediately tracked. The commit is best-effort — if git is
// unavailable or the directory is not a repo the scaffold is still written.
func EnsureBundle(vignoblePath string) error {
	wikiDir := filepath.Join(vignoblePath, "wiki")
	indexPath := filepath.Join(wikiDir, "index.md")

	// Idempotency check: already seeded.
	if _, err := os.Stat(indexPath); err == nil {
		return nil
	}

	if err := os.MkdirAll(wikiDir, 0755); err != nil {
		return fmt.Errorf("wiki: mkdir %s: %w", wikiDir, err)
	}

	files := map[string]string{
		"index.md":        "templates/index.md",
		"INSTRUCTIONS.md": "templates/INSTRUCTIONS.md",
		"log.md":          "templates/log.md",
	}
	for dst, src := range files {
		data, err := templates.ReadFile(src)
		if err != nil {
			return fmt.Errorf("wiki: read template %s: %w", src, err)
		}
		if err := os.WriteFile(filepath.Join(wikiDir, dst), data, 0644); err != nil {
			return fmt.Errorf("wiki: write %s: %w", dst, err)
		}
	}

	// Best-effort git commit so the scaffold is tracked in the vignoble repo.
	gitCommit(vignoblePath)
	return nil
}

// isGitRepo reports whether dir contains a git repository (.git dir or file).
func isGitRepo(dir string) bool {
	_, err := os.Stat(filepath.Join(dir, ".git"))
	return err == nil
}

// gitCommit stages wiki/ and commits it inside repoDir. Best-effort — errors
// are silently ignored so a missing git binary or non-repo dir doesn't abort
// the daemon.
func gitCommit(repoDir string) {
	run := func(args ...string) error {
		cmd := exec.Command("git", args...)
		cmd.Dir = repoDir
		out, err := cmd.CombinedOutput()
		if err != nil {
			_ = strings.TrimSpace(string(out)) // consume output
		}
		return err
	}

	if !isGitRepo(repoDir) {
		return
	}
	_ = run("add", "wiki/")
	_ = run("commit", "-m", "chore(wiki): seed OKF bundle scaffold")
}
