package session

import (
	"os/exec"
	"strings"
)

// tmux window helpers for the control-room topology: maîtres are windows in
// the vignoble's `conductor` tmux session (socket pinard-<vignoble>), alongside
// the dashboard window. Workers remain separate sessions on the same server.

// RegisseurWindow is the reserved tmux window name for the régisseur — the
// vignoble's general lane / estate manager (the top of the control room).
// Bracketed so no per-parcelle maître window can collide with it. Keep in sync
// with bin/pinard (`-n`) and the TS list_parcelles filter in pi-extension/pinard/index.ts.
const RegisseurWindow = "[régisseur]"

// IsReservedWindow reports whether a name (once tmux-sanitized) would collide
// with the reserved régisseur window, and therefore must not name a maître.
func IsReservedWindow(name string) bool {
	return SanitizeName(name) == RegisseurWindow
}

// HasSession reports whether a tmux session exists on the vignoble's socket.
func HasSession(vignoble, sessionName string) bool {
	socket := "pinard-" + vignoble
	return exec.Command("tmux", "-L", socket, "has-session", "-t", sessionName).Run() == nil
}

// HasWindow reports whether a window with the given name exists in the session.
func HasWindow(vignoble, sessionName, windowName string) bool {
	socket := "pinard-" + vignoble
	windowName = SanitizeName(windowName)
	out, err := exec.Command("tmux", "-L", socket, "list-windows", "-t", sessionName, "-F", "#{window_name}").Output()
	if err != nil {
		return false
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line == windowName {
			return true
		}
	}
	return false
}

// EnsureWindow creates a window running command in the session if one with that
// name does not already exist (single-maître-per-parcelle). It is a no-op when
// the window is already present. The session must already exist.
func EnsureWindow(vignoble, sessionName, windowName, command string) error {
	if HasWindow(vignoble, sessionName, windowName) {
		return nil
	}
	socket := "pinard-" + vignoble
	windowName = SanitizeName(windowName)
	if err := exec.Command("tmux", "-L", socket, "new-window", "-d", "-t", sessionName, "-n", windowName, command).Run(); err != nil {
		return err
	}
	// Cosmetic: maître window tabs get a gold tint.
	target := sessionName + ":" + windowName
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-style", "fg=colour136,bg=colour236").Run()         //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-current-style", "fg=colour232,bg=colour136").Run() //nolint:errcheck
	// 🧑‍🌾 emoji + window index on the maître tab (format keeps the gold style above).
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-format", " #I 🧑‍🌾 #W ").Run()                     //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-current-format", " #I 🧑‍🌾 #W ").Run()             //nolint:errcheck
	return nil
}

// SelectWindow focuses a window (the "attach to a parcelle" action).
func SelectWindow(vignoble, sessionName, windowName string) error {
	socket := "pinard-" + vignoble
	windowName = SanitizeName(windowName)
	return exec.Command("tmux", "-L", socket, "select-window", "-t", sessionName+":"+windowName).Run()
}
