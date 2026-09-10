package watcher

import (
	"log"
	"os/exec"
	"regexp"
	"sync"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
)

// safeNameRe is a strict allowlist for session names used in tmux/shell
// commands. Only alphanumerics, hyphens, and underscores are permitted.
// Legitimate Pinard session names (parcelle--project-id form) satisfy this.
// Any KV-supplied name that does NOT match is rejected — never sanitized-and-
// proceeded — to prevent shell injection via a crafted `name` field.
var safeNameRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// RemoteMirrorWatcher watches the pinard-agents KV and maintains local tmux
// mirror sessions for agents that are running on remote hosts. A mirror session
// runs `aoc attach <name> --vignoble-name <vignoble>` so the operator can see
// remote vendangeurs in `tmux ls` without manually running aoc attach.
//
// Mirrors are created for agents that belong to this vignoble and whose session
// does NOT exist on the local tmux socket (i.e. they are remote). Mirrors are
// torn down when the KV entry disappears or the agent reaches a terminal state.
type RemoteMirrorWatcher struct {
	Vignoble *config.Vignoble
	KV       pnats.KVReader
	AOCBin   string

	mu       sync.Mutex
	mirrored map[string]bool // session names we have created mirrors for
}

// terminalStates are agent states for which no mirror is needed.
var terminalStates = map[string]bool{
	"stopped":   true,
	"completed": true,
	"failed":    true,
}

func (r *RemoteMirrorWatcher) socket() string {
	return "pinard-" + r.Vignoble.Name
}

func (r *RemoteMirrorWatcher) Run() {
	r.mu.Lock()
	if r.mirrored == nil {
		r.mirrored = make(map[string]bool)
	}
	r.mu.Unlock()

	// Build the current set of live remote agents from KV.
	wantMirror := make(map[string]bool)

	keys, err := r.KV.Keys("pinard-agents")
	if err != nil {
		log.Printf("[remote-mirror] KV list error: %v", err)
		return
	}

	for _, key := range keys {
		data, err := r.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}

		// Only manage agents for this vignoble.
		vb, _ := data["vignoble"].(string)
		if vb != r.Vignoble.Name {
			continue
		}

		name, _ := data["name"].(string)
		if name == "" {
			name = key
		}
		name = session.SanitizeName(name)

		// Strict allowlist: reject any name containing shell metacharacters.
		// name originates from untrusted KV data written by a remote worker;
		// it reaches tmux new-session which passes the command through a shell.
		// SanitizeName only maps . : whitespace → '-'; it does NOT strip $, `,
		// ;, |, etc. Fail closed: skip and log rather than sanitize-and-proceed.
		if !safeNameRe.MatchString(name) {
			log.Printf("[remote-mirror] skipping agent %q: name contains disallowed characters", name)
			continue
		}

		// Skip terminal agents.
		state, _ := data["state"].(string)
		if terminalStates[state] {
			continue
		}

		// Skip agents whose session already exists locally — they have a real
		// tmux session on this host and need no mirror.
		if isSessionLocal(r.socket(), name) {
			continue
		}

		wantMirror[name] = true
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	// Tear down mirrors for sessions no longer in wantMirror.
	for name := range r.mirrored {
		if wantMirror[name] {
			continue
		}
		if err := r.killMirror(name); err != nil {
			log.Printf("[remote-mirror] kill mirror %q: %v", name, err)
		} else {
			log.Printf("[remote-mirror] removed mirror for %q", name)
		}
		delete(r.mirrored, name)
	}

	// Create mirrors for new remote agents.
	for name := range wantMirror {
		if r.mirrored[name] {
			// Already mirroring — ensure the session still exists (aoc attach
			// exits when the remote agent stops publishing; recreate if needed).
			if isSessionLocal(r.socket(), name) {
				continue
			}
			// Session died — respawn the mirror.
			delete(r.mirrored, name)
		}

		if err := r.spawnMirror(name); err != nil {
			log.Printf("[remote-mirror] spawn mirror %q: %v", name, err)
			continue
		}
		log.Printf("[remote-mirror] mirroring remote agent %q", name)
		r.mirrored[name] = true
	}
}

func (r *RemoteMirrorWatcher) spawnMirror(name string) error {
	aocBin := r.AOCBin
	if aocBin == "" {
		aocBin = "aoc"
	}
	// Pass the command as separate tokens to tmux new-session so that tmux
	// does NOT invoke a shell. tmux new-session interprets the trailing
	// positional arguments as argv[0..n] of the child process directly
	// (equivalent to execvp), bypassing sh entirely. This prevents any shell
	// metacharacter in name / vignoble from being interpreted even if the
	// strict allowlist above were somehow bypassed.
	return exec.Command(
		"tmux", "-L", r.socket(),
		"new-session", "-d", "-s", name,
		aocBin, "attach", name, "--vignoble-name", r.Vignoble.Name, "--timeout", "0",
	).Run()
}

func (r *RemoteMirrorWatcher) killMirror(name string) error {
	return exec.Command(
		"tmux", "-L", r.socket(),
		"kill-session", "-t", name,
	).Run()
}

// isSessionLocal reports whether a tmux session named `name` exists on the
// given socket, i.e. the agent is running on this host.
func isSessionLocal(socket, name string) bool {
	return exec.Command("tmux", "-L", socket, "has-session", "-t", name).Run() == nil
}
