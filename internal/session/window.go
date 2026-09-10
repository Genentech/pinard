package session

import (
	"fmt"
	"os/exec"
	"strconv"
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

// sessionDimensions queries the attached-client dimensions for a tmux session.
// Returns (width, height, ok). ok is false when no client is attached or the
// query fails — callers should treat that as "unknown, skip resize".
func sessionDimensions(socket, sessionName string) (int, int, bool) {
	out, err := exec.Command("tmux", "-L", socket, "display-message", "-t", sessionName, "-p", "#{session_width} #{session_height}").Output()
	if err != nil {
		return 0, 0, false
	}
	parts := strings.Fields(strings.TrimSpace(string(out)))
	if len(parts) != 2 {
		return 0, 0, false
	}
	w, err1 := strconv.Atoi(parts[0])
	h, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil || w <= 0 || h <= 0 {
		return 0, 0, false
	}
	return w, h, true
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
	target := sessionName + ":" + windowName
	// Resize the new window to the session's attached-client dimensions immediately
	// so that the TUI (pi) boots at the correct size and is not stuck at the
	// detached-window default (80×24). If no client is attached, skip — the
	// SelectWindow nudge below will correct it when the window is first selected.
	if w, h, ok := sessionDimensions(socket, sessionName); ok {
		exec.Command("tmux", "-L", socket, "resize-window", "-t", target, "-x", fmt.Sprintf("%d", w), "-y", fmt.Sprintf("%d", h)).Run() //nolint:errcheck
	}
	// Cosmetic: maître window tabs get a gold tint.
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-style", "fg=colour136,bg=colour236").Run()         //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-current-style", "fg=colour232,bg=colour136").Run() //nolint:errcheck
	// 🧑‍🌾 emoji + window index on the maître tab (format keeps the gold style above).
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-format", " #I 🧑‍🌾 #W ").Run()                     //nolint:errcheck
	exec.Command("tmux", "-L", socket, "set-window-option", "-t", target, "window-status-current-format", " #I 🧑‍🌾 #W ").Run()             //nolint:errcheck
	return nil
}

// SelectWindow focuses a window (the "attach to a parcelle" action).
// It applies a 1-row shrink→grow nudge after selecting so that full-screen TUIs
// (e.g. pi) reliably repaint to fill the window. This is idempotent and
// flicker-free: pi only redraws on an actual size change, so the brief −1/+1
// cycle is necessary but invisible to the user.
func SelectWindow(vignoble, sessionName, windowName string) error {
	socket := "pinard-" + vignoble
	windowName = SanitizeName(windowName)
	target := sessionName + ":" + windowName
	if err := exec.Command("tmux", "-L", socket, "select-window", "-t", target).Run(); err != nil {
		return err
	}
	// Nudge: shrink by 1 row then restore, triggering a SIGWINCH size-change
	// event so the TUI repaints to full height regardless of spawn-time sizing.
	_, h, ok := sessionDimensions(socket, sessionName)
	if ok && h > 1 {
		exec.Command("tmux", "-L", socket, "resize-window", "-t", target, "-y", fmt.Sprintf("%d", h-1)).Run() //nolint:errcheck
		exec.Command("tmux", "-L", socket, "resize-window", "-t", target, "-y", fmt.Sprintf("%d", h)).Run()   //nolint:errcheck
	}
	return nil
}
