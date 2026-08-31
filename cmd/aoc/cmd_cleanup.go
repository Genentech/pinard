package main

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

var cleanupCmd = &cobra.Command{
	Use:   "cleanup",
	Short: "Clean stale worktrees and archive completed openspec changes",
	RunE: func(cmd *cobra.Command, args []string) error {
		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		nc := pnats.NewClient(creds)
		defer nc.Close()

		// Clean worktrees in each vigne
		for vigneName, vigne := range vb.Config.Vignes {
			vignePath := vigne.ExpandedPath()
			if _, err := os.Stat(vignePath); err != nil {
				continue
			}
			cleanWorktrees(vignePath, vigneName, vb.Name)
			reviewOpenspec(vignePath, vigneName, vb.Name, nc)
		}

		// Also review openspec changes in the vignoble itself
		reviewOpenspec(vb.Path, vb.Name, vb.Name, nc)

		return nil
	},
}

var cleanupArchiveCmd = &cobra.Command{
	Use:   "archive",
	Short: "Archive a specific openspec change",
	RunE: func(cmd *cobra.Command, args []string) error {
		project, _ := cmd.Flags().GetString("project")
		change, _ := cmd.Flags().GetString("change")

		if project == "" || change == "" {
			return fmt.Errorf("--project and --change are required")
		}

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		var vignePath string
		if project == vb.Name {
			// Vignoble-level change
			vignePath = vb.Path
		} else if vigne, ok := vb.Config.Vignes[project]; ok {
			vignePath = vigne.ExpandedPath()
		} else {
			return fmt.Errorf("project %q not found", project)
		}
		changesDir := filepath.Join(vignePath, "changes", change)
		archiveDir := filepath.Join(vignePath, "changes", ".archive", change)

		if _, err := os.Stat(changesDir); err != nil {
			return fmt.Errorf("change %q not found in %s", change, project)
		}

		os.MkdirAll(filepath.Dir(archiveDir), 0755)
		if err := os.Rename(changesDir, archiveDir); err != nil {
			return fmt.Errorf("archive failed: %w", err)
		}

		fmt.Printf("Archived: %s/%s → .archive/%s\n", project, change, change)
		return nil
	},
}

// abandonedAfter is how long a worktree with no live worker may sit idle
// before cleanup reaps it. Workers are short-lived (minutes); anything older
// than this with no tmux session is finished or abandoned. Its work is either
// merged on GitLab (squash/rebase merges leave the branch a non-ancestor of
// local main, so isBranchMerged can't see it) or orphan-recovery will respawn
// from spawn.json — either way the local worktree is disposable.
const abandonedAfter = 12 * time.Hour

func cleanWorktrees(vignePath, vigneName, vignoble string) {
	worktreesDir := filepath.Join(vignePath, ".worktrees")
	entries, err := os.ReadDir(worktreesDir)
	if err != nil {
		return
	}

	removed := false
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		wtPath := filepath.Join(worktreesDir, entry.Name())

		// Get the branch name for this worktree
		branch, err := worktreeBranch(wtPath)
		if err != nil {
			continue
		}

		// Never touch a worktree whose worker is still live.
		if tmuxSessionAlive(vignoble, entry.Name()) {
			continue
		}

		merged := isBranchMerged(vignePath, branch)
		abandoned := false
		if info, statErr := entry.Info(); statErr == nil {
			abandoned = time.Since(info.ModTime()) > abandonedAfter
		}

		if merged || abandoned {
			reason := "abandoned"
			if merged {
				reason = "merged"
			}
			log.Printf("[cleanup] Removing %s worktree: %s/%s (branch: %s)", reason, vigneName, entry.Name(), branch)
			exec.Command("git", "-C", vignePath, "worktree", "remove", wtPath, "--force").Run()
			exec.Command("git", "-C", vignePath, "branch", "-D", branch).Run()
			removed = true
		}
	}

	if removed {
		exec.Command("git", "-C", vignePath, "worktree", "prune").Run()
	}
}

// tmuxSessionAlive reports whether a worker tmux session of the given name is
// still running under the vignoble socket.
func tmuxSessionAlive(vignoble, name string) bool {
	socket := "pinard-" + vignoble
	return exec.Command("tmux", "-L", socket, "has-session", "-t", name).Run() == nil
}

func worktreeBranch(wtPath string) (string, error) {
	out, err := exec.Command("git", "-C", wtPath, "rev-parse", "--abbrev-ref", "HEAD").Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

func isBranchMerged(repoPath, branch string) bool {
	out, err := exec.Command("git", "-C", repoPath, "branch", "--merged", "main").Output()
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(out), "\n") {
		if strings.TrimSpace(line) == branch || strings.TrimSpace(line) == "* "+branch {
			return true
		}
	}
	return false
}

var taskRe = regexp.MustCompile(`- \[([ x])\]`)

func reviewOpenspec(vignePath, vigneName, vignoble string, nc *pnats.Client) {
	changesDir := filepath.Join(vignePath, "changes")
	entries, err := os.ReadDir(changesDir)
	if err != nil {
		return
	}

	for _, entry := range entries {
		if !entry.IsDir() || entry.Name() == ".archive" || entry.Name() == ".gitkeep" {
			continue
		}

		tasksPath := filepath.Join(changesDir, entry.Name(), "tasks.md")
		data, err := os.ReadFile(tasksPath)
		if err != nil {
			continue
		}

		matches := taskRe.FindAllStringSubmatch(string(data), -1)
		if len(matches) == 0 {
			continue
		}

		done := 0
		for _, m := range matches {
			if m[1] == "x" {
				done++
			}
		}
		total := len(matches)
		pct := (done * 100) / total

		if pct == 100 {
			// Fully done — archive automatically
			archiveDir := filepath.Join(changesDir, ".archive", entry.Name())
			os.MkdirAll(filepath.Dir(archiveDir), 0755)
			if err := os.Rename(filepath.Join(changesDir, entry.Name()), archiveDir); err == nil {
				log.Printf("[cleanup] Archived completed openspec: %s/%s (%d/%d tasks)", vigneName, entry.Name(), done, total)
			}
		} else {
			// Not 100% — notify conductor for review
			log.Printf("[cleanup] Openspec %s/%s: %d%% done (%d/%d tasks) — notifying conductor", vigneName, entry.Name(), pct, done, total)
			if nc != nil {
				nc.Publish(fmt.Sprintf("pinard.%s.notifications", vignoble), map[string]any{
					"message":   fmt.Sprintf("[cleanup] Openspec '%s' on %s is %d%% done (%d/%d tasks). Review with: aoc cleanup archive --project %s --change %s", entry.Name(), vigneName, pct, done, total, vigneName, entry.Name()),
					"timestamp": "",
				})
			}
		}
	}
}

func init() {
	cleanupArchiveCmd.Flags().String("project", "", "Project name")
	cleanupArchiveCmd.Flags().String("change", "", "Change name to archive")
	cleanupCmd.AddCommand(cleanupArchiveCmd)
	rootCmd.AddCommand(cleanupCmd)
}
