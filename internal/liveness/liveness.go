// Package liveness determines, from OS ground truth, which babysitter runs have
// a worker process alive right now.
//
// It deliberately does NOT trust KV/registry state: workers often fail to
// publish a "stopped" status (leaving stale "running" entries), and a missing
// entry for a run that is actually alive would let a duplicate worker through.
// Instead it reads ground truth from the OS — for each live tmux session under
// the vignoble socket, it walks the process tree and reads BABYSITTER_RUN_ID
// directly from each process's /proc/<pid>/environ.
//
// LIMITATION: local-host only. It cannot see workers on remote compute (k8s
// pods, other VMs). When remote workers land, add a portable mechanism — e.g.
// a heartbeat-refreshed NATS KV lease keyed by run ID with a short TTL, so the
// entry auto-expires on crash/OOM/exit without relying on the worker to
// announce its own death. Until then, /proc inspection is authoritative for the
// single-host topology, and is the one source both spawn (duplicate guard) and
// orphan-recovery (orphan detection) must share.
package liveness

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// WorkerForRun returns the tmux session name of a live worker for runID, or ""
// if none is alive.
func WorkerForRun(vignobleName, runID string) string {
	socket := "pinard-" + vignobleName
	for _, sess := range listSessions(socket) {
		if sess == "" || sess == "conductor" {
			continue
		}
		for _, pid := range sessionPanePIDs(socket, sess) {
			if procTreeHasRunID(pid, runID) {
				return sess
			}
		}
	}
	return ""
}

// LiveRunIDs returns the set of run IDs that have a worker alive right now.
// It walks every session's process tree once, so callers that need to test many
// runs (e.g. orphan-recovery) can do O(1) map lookups instead of one tmux+/proc
// walk per run.
func LiveRunIDs(vignobleName string) map[string]bool {
	live := make(map[string]bool)
	socket := "pinard-" + vignobleName
	for _, sess := range listSessions(socket) {
		if sess == "" || sess == "conductor" {
			continue
		}
		for _, pid := range sessionPanePIDs(socket, sess) {
			collectRunIDs(pid, live)
		}
	}
	return live
}

func listSessions(socket string) []string {
	out, err := exec.Command("tmux", "-L", socket, "list-sessions", "-F", "#{session_name}").Output()
	if err != nil {
		return nil // no server / no sessions — nothing alive
	}
	return strings.Split(strings.TrimSpace(string(out)), "\n")
}

func sessionPanePIDs(socket, sess string) []string {
	out, err := exec.Command("tmux", "-L", socket, "list-panes", "-t", sess, "-F", "#{pane_pid}").Output()
	if err != nil {
		return nil
	}
	return strings.Fields(string(out))
}

// procTreeHasRunID reports whether the process with the given PID, or any of its
// descendants, has BABYSITTER_RUN_ID=runID in its environment.
func procTreeHasRunID(pid, runID string) bool {
	if runIDOf(pid) == runID {
		return true
	}
	for _, child := range procChildren(pid) {
		if procTreeHasRunID(child, runID) {
			return true
		}
	}
	return false
}

// collectRunIDs records the BABYSITTER_RUN_ID of pid and all its descendants.
func collectRunIDs(pid string, set map[string]bool) {
	if rid := runIDOf(pid); rid != "" {
		set[rid] = true
	}
	for _, child := range procChildren(pid) {
		collectRunIDs(child, set)
	}
}

func procChildren(pid string) []string {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%s/task/%s/children", pid, pid))
	if err != nil {
		return nil
	}
	return strings.Fields(string(data))
}

// runIDOf returns the BABYSITTER_RUN_ID of a single process, or "" if unset.
func runIDOf(pid string) string {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%s/environ", pid))
	if err != nil {
		return ""
	}
	for _, kv := range strings.Split(string(data), "\x00") {
		if v, ok := strings.CutPrefix(kv, "BABYSITTER_RUN_ID="); ok {
			return v
		}
	}
	return ""
}
