package git

import (
	"fmt"
	"os/exec"
	"strings"
)

func run(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	out, err := cmd.CombinedOutput()
	output := strings.TrimSpace(string(out))
	if err != nil && output != "" {
		return output, fmt.Errorf("%w: %s", err, output)
	}
	return output, err
}

func Fetch(dir string) error {
	_, err := run(dir, "fetch", "--quiet")
	return err
}

func Pull(dir string) error {
	_, err := run(dir, "pull", "--ff-only", "--quiet")
	return err
}

func Branch(dir, name, startPoint string) error {
	_, err := run(dir, "branch", name, startPoint)
	return err
}

func BranchTrack(dir, name, remote string) error {
	_, err := run(dir, "branch", "--track", name, remote)
	return err
}

func WorktreeAdd(dir, path, branch, startPoint string) error {
	_, err := run(dir, "worktree", "add", path, "-b", branch, startPoint)
	return err
}

func WorktreeRemove(dir, path string) error {
	_, err := run(dir, "worktree", "remove", path, "--force")
	return err
}

func CurrentBranch(dir string) (string, error) {
	return run(dir, "rev-parse", "--abbrev-ref", "HEAD")
}

func RemoteURL(dir string) (string, error) {
	return run(dir, "remote", "get-url", "origin")
}

func Tags(dir string) ([]string, error) {
	out, err := run(dir, "tag", "--sort=-v:refname")
	if err != nil || out == "" {
		return nil, err
	}
	return strings.Split(out, "\n"), nil
}

func FetchTags(dir string) error {
	_, err := run(dir, "fetch", "--tags", "--quiet")
	return err
}

func MergeCommitSHA(dir, ref string) (string, error) {
	return run(dir, "rev-parse", ref)
}

// RemoteBranchExists reports whether the named branch exists on the given remote.
func RemoteBranchExists(dir, remote, branch string) bool {
	cmd := exec.Command("git", "-C", dir, "ls-remote", "--exit-code", remote, "refs/heads/"+branch)
	return cmd.Run() == nil
}

// DefaultBranch returns the default branch name of origin (e.g. "main" or "master").
// It resolves origin/HEAD; falls back to "main" if unset.
func DefaultBranch(dir string) (string, error) {
	out, err := run(dir, "rev-parse", "--abbrev-ref", "origin/HEAD")
	if err != nil || out == "" || out == "origin/HEAD" {
		// origin/HEAD may not be set; fall back to main.
		return "main", nil
	}
	// Strip the "origin/" prefix to get just the branch name.
	return strings.TrimPrefix(out, "origin/"), nil
}

// EnsureRemoteBranch creates the named branch on origin (off the default branch)
// if it does not already exist, then fetches it so the local remote-tracking ref
// is up to date. It is idempotent and race-safe: a push rejected because another
// concurrent caller already created the branch is treated as success.
func EnsureRemoteBranch(dir, branch string) error {
	if RemoteBranchExists(dir, "origin", branch) {
		return nil
	}

	defaultBranch, err := DefaultBranch(dir)
	if err != nil {
		return fmt.Errorf("resolve default branch: %w", err)
	}

	// Fetch so we have origin/<defaultBranch> up to date.
	if _, err := run(dir, "fetch", "origin", defaultBranch); err != nil {
		return fmt.Errorf("fetch origin/%s: %w", defaultBranch, err)
	}

	// Push the default branch HEAD as the new branch. --no-verify skips hooks.
	// If another concurrent spawn already pushed it, git exits non-zero with
	// "already exists" in stderr — treat that as success.
	refspec := fmt.Sprintf("refs/remotes/origin/%s:refs/heads/%s", defaultBranch, branch)
	cmd := exec.Command("git", "-C", dir, "push", "origin", refspec)
	out, pushErr := cmd.CombinedOutput()
	if pushErr != nil {
		outStr := strings.ToLower(string(out))
		if strings.Contains(outStr, "already exists") || strings.Contains(outStr, "already up-to-date") {
			// Another concurrent spawn already created it — not an error.
			pushErr = nil
		} else {
			return fmt.Errorf("push %s to origin: %w: %s", branch, pushErr, strings.TrimSpace(string(out)))
		}
	}

	// Fetch the new branch so origin/<branch> is resolvable locally.
	if _, err := run(dir, "fetch", "origin", branch); err != nil {
		return fmt.Errorf("fetch origin/%s after create: %w", branch, err)
	}

	return nil
}
