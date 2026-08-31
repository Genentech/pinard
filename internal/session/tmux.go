package session

import (
	"fmt"
	"os/exec"
	"strings"
)

type tmuxManager struct{}

func newTmux() *tmuxManager {
	return &tmuxManager{}
}

func (t *tmuxManager) SpawnWorker(workspace, name, command string) error {
	socket := "pinard-" + workspace
	name = SanitizeName(name)
	if err := exec.Command("tmux", "-L", socket, "new-session", "-d", "-s", name, command).Run(); err != nil {
		return err
	}
	// Cosmetic: vendangeur sessions get a whole-bar leaf-green status line. Set
	// both status-style AND the deprecated status-bg/status-fg: in tmux they are
	// decoupled and the EMPTY fill of the bar is drawn with status-bg, so setting
	// status-style alone would leave the fill grey (the global base).
	exec.Command("tmux", "-L", socket, "set-option", "-t", name, "status-style", "bg=colour22,fg=colour231").Run()         //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-option", "-t", name, "status-bg", "colour22").Run()                          //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-option", "-t", name, "status-fg", "colour231").Run()                        //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-option", "-t", name, "status-left", " 🧺 vendangeur ").Run()                  //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-option", "-t", name, "status-left-style", "bg=colour22,fg=colour231").Run()    //nolint:errcheck
	return nil
}

func (t *tmuxManager) StopWorker(workspace, name string) error {
	socket := "pinard-" + workspace
	name = SanitizeName(name)
	return exec.Command("tmux", "-L", socket, "kill-session", "-t", name).Run()
}

func (t *tmuxManager) GetWorkerCwd(workspace, name string) (string, error) {
	socket := "pinard-" + workspace
	name = SanitizeName(name)
	out, err := exec.Command("tmux", "-L", socket, "display-message", "-t", name, "-p", "#{pane_current_path}").Output()
	if err != nil {
		return "", fmt.Errorf("tmux get cwd: %w", err)
	}
	return strings.TrimSpace(string(out)), nil
}

func (t *tmuxManager) Close() error {
	return nil
}
