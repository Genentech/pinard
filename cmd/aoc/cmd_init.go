package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/Genentech/pinard/internal/config"
	"github.com/spf13/cobra"
)

var initCmd = &cobra.Command{
	Use:   "init [name]",
	Short: "Scaffold a new vignoble directory",
	RunE: func(cmd *cobra.Command, args []string) error {
		gitlabHost, _ := cmd.Flags().GetString("gitlab-host")
		gitlabGroup, _ := cmd.Flags().GetString("gitlab-group")
		targetPath, _ := cmd.Flags().GetString("path")
		local, _ := cmd.Flags().GetBool("local")

		name := ""
		if len(args) > 0 {
			name = args[0]
		}

		// Infer from cwd if in an existing vignoble
		if name == "" {
			if _, err := os.Stat("vignes.yaml"); err == nil {
				cwd, _ := os.Getwd()
				targetPath = cwd
				name = strings.TrimPrefix(filepath.Base(cwd), "vignoble-")
			}
		}

		if name == "" {
			return fmt.Errorf("name is required (or run from an existing vignoble directory)")
		}
		// --local mode: no GitLab host required (services are local, no cluster)
		if !local && gitlabHost == "" && targetPath == "" {
			return fmt.Errorf("--gitlab-host is required for new vignobles")
		}

		home, _ := os.UserHomeDir()
		if targetPath == "" {
			targetPath = filepath.Join(home, "vignoble-"+name)
		}
		targetPath = expandHome(targetPath)

		isUpdate := false
		if _, err := os.Stat(targetPath); err == nil {
			isUpdate = true
			fmt.Printf("Updating vignoble '%s' at %s...\n", name, targetPath)
		} else {
			fmt.Printf("Creating vignoble '%s' at %s...\n", name, targetPath)
		}
		_ = isUpdate

		// Create directories
		for _, dir := range []string{"changes", ".state", "logs", "vignes"} {
			os.MkdirAll(filepath.Join(targetPath, dir), 0755)
		}
		touchFile(filepath.Join(targetPath, "changes", ".gitkeep"))

		// vignes.yaml
		vignesPath := filepath.Join(targetPath, "vignes.yaml")
		if _, err := os.Stat(vignesPath); err != nil {
			var content string
			if local {
				content = soloVignesYAML(gitlabHost, gitlabGroup)
			} else {
				content = fmt.Sprintf("gitlab_host: %s\n", gitlabHost)
				if gitlabGroup != "" {
					content += fmt.Sprintf("gitlab_group: %s\n", gitlabGroup)
				}
				content += "\nvignes: {}\n"
			}
			os.WriteFile(vignesPath, []byte(content), 0644)
			fmt.Println("  created vignes.yaml")
		} else {
			fmt.Println("  vignes.yaml exists")
		}

		// Solo mode: write ~/.config/pinard/credentials.yaml with localhost endpoints.
		// Done early (before daemon start) so the daemon can pick up the config.
		if local {
			if err := writeSoloCredentials(); err != nil {
				fmt.Fprintf(os.Stderr, "  warning: could not write solo credentials.yaml: %v\n", err)
			} else {
				fmt.Println("  ~/.config/pinard/credentials.yaml (solo profile)")
			}
		}

		// .gitignore
		gitignorePath := filepath.Join(targetPath, ".gitignore")
		ensureGitignoreEntries(gitignorePath, []string{".state/", "logs/", "vignes/*/sessions/", ".pi/", ".engram/"})
		fmt.Println("  .gitignore")

		// PINARD.md symlink
		exe, _ := os.Executable()
		pinardDir := filepath.Dir(filepath.Dir(resolveSymlink(exe)))
		pinardMD := filepath.Join(pinardDir, "PINARD.md")
		if _, err := os.Stat(pinardMD); err == nil {
			target := filepath.Join(targetPath, "PINARD.md")
			os.Remove(target)
			os.Symlink(pinardMD, target)
			fmt.Println("  PINARD.md")
		}

		// install.sh symlink → pinard repo install script
		installSrc := filepath.Join(pinardDir, "install")
		if _, err := os.Stat(installSrc); err == nil {
			installDst := filepath.Join(targetPath, "install.sh")
			os.Remove(installDst)
			os.Symlink(installSrc, installDst)
			fmt.Println("  install.sh")
		}

		// Pi permissions policy for conductor (project-level)
		piPolicyDir := filepath.Join(targetPath, ".pi", "agent")
		os.MkdirAll(piPolicyDir, 0755)
		piPolicyPath := filepath.Join(piPolicyDir, "pi-permissions.jsonc")
		if _, err := os.Stat(piPolicyPath); err != nil {
			conductorPolicy := `{
  // Conductor permissions — bash gated by command pattern
  "defaultPolicy": {
    "bash": "ask"
  },
  "bash": {
    "*": "allow"
  },
  "special": {
    "external_directory": "allow"
  }
}
`
			os.WriteFile(piPolicyPath, []byte(conductorPolicy), 0644)
			fmt.Println("  .pi/agent/pi-permissions.jsonc")
		} else {
			fmt.Println("  .pi/agent/pi-permissions.jsonc exists")
		}

		// Migrate off systemd: tear down any units a previous version installed.
		// The daemon now self-supervises (PID file) and self-reloads (mtime
		// poll + re-exec), so these units would otherwise run a second daemon.
		removeLegacySystemdUnits(name)

		// Symlink pinard-picker into ~/.local/bin if not already there
		pickerSrc := filepath.Join(pinardDir, "bin", "pinard-picker")
		pickerDst := filepath.Join(home, ".local", "bin", "pinard-picker")
		if _, err := os.Stat(pickerSrc); err == nil {
			if _, err := os.Lstat(pickerDst); err != nil {
				os.MkdirAll(filepath.Dir(pickerDst), 0755)
				os.Symlink(pickerSrc, pickerDst)
			}
			fmt.Println("  pinard-picker symlinked")
		}

		// Ensure tmux config is sourced in ~/.tmux.conf
		tmuxConf := filepath.Join(pinardDir, "etc", "tmux.conf")
		if _, err := os.Stat(tmuxConf); err == nil {
			ensureTmuxSourced(tmuxConf)
			fmt.Println("  tmux: prefix+f picker configured")
		}

		if local {
			// Solo mode: aoc connects only — services are owned by the native app
			// (macOS) or the Docker container (Linux). Do not start the daemon here;
			// the user runs `pinard up` to bring services up first.
			if !isUpdate {
				fmt.Printf("\nDone. Next steps:\n")
				fmt.Printf("  1. Start local services:  pinard up\n")
				fmt.Printf("  2. Set your LLM key:      echo 'export ANTHROPIC_API_KEY=sk-...' >> ~/.config/pinard/env\n")
				fmt.Printf("  3. Start the conductor:   cd %s && pinard\n", targetPath)
				fmt.Printf("\nEdit ~/.config/pinard/credentials.yaml to fill in GitLab details.\n")
			}
		} else {
			// Start (or restart, to pick up a new binary) the self-supervising
			// daemon. Best-effort: a missing credentials.yaml just means the child
			// exits and the user starts it later with `aoc daemon start`.
			startDaemon := exec.Command(selfPath(), "daemon", "restart")
			startDaemon.Dir = targetPath
			startDaemon.Stdout = os.Stdout
			startDaemon.Stderr = os.Stderr
			startDaemon.Run()

			if !isUpdate {
				fmt.Printf("\nDone. Daemon started. Start the conductor:\n")
				fmt.Printf("  cd %s && pinard\n", targetPath)
				fmt.Printf("\nManage the daemon with: aoc daemon {start,stop,restart,status}\n")
			}
		}
		return nil
	},
}

// writeSoloCredentials writes ~/.config/pinard/credentials.yaml with localhost
// endpoints suitable for solo-mode operation (pinard-services Docker image or
// macOS native ServiceOrchestrator app). Reads port overrides from the macOS
// config.json when present; uses defaults elsewhere. Preserves an existing file.
func writeSoloCredentials() error {
	home, _ := os.UserHomeDir()
	credsPath := filepath.Join(home, ".config", "pinard", "credentials.yaml")

	if _, err := os.Stat(credsPath); err == nil {
		// Already exists — do not overwrite; user may have customised it.
		return nil
	}

	sc := config.ReadSoloConfig()

	content := fmt.Sprintf(`# Pinard solo profile — localhost endpoints
# Generated by: aoc init --local
# Services are managed externally (pinard up / pinard down).
# aoc connects only — it never supervises these services.

gitlab:
  host: CHANGE_ME          # e.g. gitlab.example.com
  user: pinard
  token_env: PINARD_GITLAB_TOKEN
  ssh_key: ~/.ssh/pinard_id_ed25519
  git_name: Pinard
  git_email: pinard@localhost

nats:
  url: nats://127.0.0.1:%d
  # No user/password — local NATS runs without auth in solo mode.

# engram is managed by the Pinard daemon (aoc daemon); no cloud server in solo mode.
# engram:
#   server: ''  # leave unset for local-only operation

# SurrealDB URL for the memory layer (memory-ingester connects here).
# surrealdb:
#   url: ws://127.0.0.1:%d

webterm:
  base_url: http://127.0.0.1:%d
  # No link/grant secrets — local webterm runs without auth in solo mode.

# BYO LLM key: add your Anthropic or OpenAI key to ~/.config/pinard/env:
#   export ANTHROPIC_API_KEY=sk-ant-...
# pi will use the direct provider (no proxy) in solo mode.
`, sc.NATSPort, sc.SurrealPort, sc.WebtermPort)

	dir := filepath.Dir(credsPath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	if err := os.WriteFile(credsPath, []byte(content), 0600); err != nil {
		return err
	}
	os.Chmod(dir, 0700)
	return nil
}

// soloVignesYAML returns the vignes.yaml content for a solo-mode vignoble.
func soloVignesYAML(gitlabHost, gitlabGroup string) string {
	var b strings.Builder
	if gitlabHost != "" {
		fmt.Fprintf(&b, "gitlab_host: %s\n", gitlabHost)
	} else {
		b.WriteString("# gitlab_host: gitlab.example.com\n")
	}
	if gitlabGroup != "" {
		fmt.Fprintf(&b, "gitlab_group: %s\n", gitlabGroup)
	}
	b.WriteString(`
models:
  worker:
    # Solo mode uses pi's direct provider with your BYO API key.
    # Set ANTHROPIC_API_KEY in ~/.config/pinard/env.
    id: claude-opus-4-5

vignes: {}
`)
	return b.String()
}

// removeLegacySystemdUnits tears down the systemd user units that older Pinard
// versions installed for this vignoble (daemon service + binary/config path
// watchers). It is best-effort: if systemctl is absent or the units never
// existed, the calls are harmless no-ops. The daemon now self-supervises and
// self-reloads, so leaving these enabled would run a duplicate daemon.
func removeLegacySystemdUnits(name string) {
	home, _ := os.UserHomeDir()
	userSystemd := filepath.Join(home, ".config", "systemd", "user")

	units := []string{
		"pinard-" + name + ".service",
		fmt.Sprintf("pinard-%s-config-watcher.path", name),
		fmt.Sprintf("pinard-%s-config-watcher.service", name),
		"pinard-aoc-watcher.path",
		"pinard-aoc-watcher.service",
	}

	if _, err := exec.LookPath("systemctl"); err == nil {
		for _, u := range units {
			exec.Command("systemctl", "--user", "disable", "--now", u).Run()
		}
	}
	for _, u := range units {
		os.Remove(filepath.Join(userSystemd, u))
	}
	if _, err := exec.LookPath("systemctl"); err == nil {
		exec.Command("systemctl", "--user", "daemon-reload").Run()
	}
}

func expandHome(path string) string {
	if strings.HasPrefix(path, "~/") {
		home, _ := os.UserHomeDir()
		return filepath.Join(home, path[2:])
	}
	return path
}

func touchFile(path string) {
	if _, err := os.Stat(path); err != nil {
		os.WriteFile(path, nil, 0644)
	}
}

func resolveSymlink(path string) string {
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return path
	}
	return resolved
}

func ensureTmuxSourced(tmuxConf string) {
	home, _ := os.UserHomeDir()
	tmuxRC := filepath.Join(home, ".tmux.conf")
	sourceLine := fmt.Sprintf("source-file %s", tmuxConf)

	if data, err := os.ReadFile(tmuxRC); err == nil {
		if strings.Contains(string(data), sourceLine) {
			return
		}
	}

	f, err := os.OpenFile(tmuxRC, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString("\n# Pinard session picker (prefix+f)\n" + sourceLine + "\n")
}

func ensureGitignoreEntries(path string, entries []string) {
	existing := ""
	if data, err := os.ReadFile(path); err == nil {
		existing = string(data)
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	for _, entry := range entries {
		if !strings.Contains(existing, entry) {
			f.WriteString(entry + "\n")
		}
	}
}

func init() {
	initCmd.Flags().String("gitlab-host", "", "GitLab hostname (required)")
	initCmd.Flags().String("gitlab-group", "", "GitLab group path")
	initCmd.Flags().String("path", "", "Target directory")
	initCmd.Flags().Bool("local", false, "Write a solo profile credentials.yaml with localhost endpoints (solo mode)")
	rootCmd.AddCommand(initCmd)
}
