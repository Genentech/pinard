package webterm

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/creack/pty"
	"github.com/nats-io/nats.go"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
)

// Flow-control tuning. Terminal output is coalesced over short windows and
// rate-capped inside a bounded buffer so a runaway `cat`/`yes` cannot flood a
// viewer or the NATS connection.
const (
	flushInterval = 16 * time.Millisecond
	maxMsgBytes   = 32 * 1024       // max bytes per NATS output message
	maxBufBytes   = 256 * 1024      // bounded per-viewer buffer; overflow is dropped
	rateCapPerSec = 2 * 1024 * 1024 // ~2 MiB/s output ceiling per viewer
	throttleMark  = "\r\n\x1b[33m…output throttled…\x1b[0m\r\n"
)

// Responder attaches to local tmux targets on request and streams the PTY over
// NATS. It runs on the pinard host (daemon-managed) and on standalone/HPC worker
// hosts (`aoc webterm-responder`).
type Responder struct {
	NC          *nats.Conn
	Vignoble    string
	GrantSecret []byte
	MaxViewers  int
	IdleTimeout time.Duration
	Socket      string // tmux socket; defaults to "pinard-<vignoble>"

	// KV is used to resolve a session's parcelle for interrupt routing.
	// Optional: if nil (or if the session is not found), interrupt is a no-op
	// (logged). With KV wired this should not be reached for live sessions.
	KV           pnats.KVReader
	AgentsBucket string // defaults to "pinard-agents"

	active int32 // atomic count of live viewers
}

func (r *Responder) socket() string {
	if r.Socket != "" {
		return r.Socket
	}
	return "pinard-" + r.Vignoble
}

func (r *Responder) agentsBucket() string {
	if r.AgentsBucket != "" {
		return r.AgentsBucket
	}
	return "pinard-agents"
}

// resolveInterruptSubject looks up the parcelle for a session name via the
// pinard-agents KV bucket, then returns the NATS interrupt subject. Returns
// empty string if resolution fails (caller should fall back).
func (r *Responder) resolveInterruptSubject(sessionName string) string {
	if r.KV == nil {
		return ""
	}
	keys, err := r.KV.Keys(r.agentsBucket())
	if err != nil {
		return ""
	}
	for _, key := range keys {
		data, err := r.KV.Get(r.agentsBucket(), key)
		if err != nil || data == nil {
			continue
		}
		name, _ := data["name"].(string)
		if name != sessionName {
			continue
		}
		parcelle, _ := data["parcelle"].(string)
		if parcelle == "" {
			continue
		}
		return pnats.AgentInterruptSubject(r.Vignoble, parcelle, sessionName)
	}
	return ""
}

func (r *Responder) maxViewers() int {
	if r.MaxViewers > 0 {
		return r.MaxViewers
	}
	return 8
}

func (r *Responder) idleTimeout() time.Duration {
	if r.IdleTimeout > 0 {
		return r.IdleTimeout
	}
	return 10 * time.Minute
}

// Run subscribes to the request subject and serves viewers until ctx is done.
func (r *Responder) Run(ctx context.Context) error {
	if r.NC == nil {
		return fmt.Errorf("webterm responder: nil NATS connection")
	}
	if len(r.GrantSecret) == 0 {
		return fmt.Errorf("webterm responder: no grant secret")
	}
	sub, err := r.NC.Subscribe(ReqSubject(r.Vignoble), func(m *nats.Msg) {
		r.handleRequest(ctx, m)
	})
	if err != nil {
		return fmt.Errorf("webterm responder subscribe: %w", err)
	}
	listSub, err := r.NC.Subscribe(ListSubject(r.Vignoble), func(m *nats.Msg) {
		r.handleList(m)
	})
	if err != nil {
		_ = sub.Unsubscribe()
		return fmt.Errorf("webterm responder list subscribe: %w", err)
	}
	log.Printf("[webterm] responder listening on %s (socket %s)", ReqSubject(r.Vignoble), r.socket())
	<-ctx.Done()
	_ = sub.Unsubscribe()
	_ = listSub.Unsubscribe()
	return ctx.Err()
}

func (r *Responder) reply(m *nats.Msg, ok bool, reason string) {
	if m.Reply == "" {
		return
	}
	data, _ := json.Marshal(ReqReply{OK: ok, Reason: reason})
	_ = m.Respond(data)
}

func (r *Responder) handleRequest(ctx context.Context, m *nats.Msg) {
	var req ReqMsg
	if err := json.Unmarshal(m.Data, &req); err != nil {
		r.reply(m, false, "bad request")
		return
	}
	// Verify the gateway grant BEFORE attaching — the sole trust gate host-side.
	// On failure stay SILENT (do not reply): the request is broadcast to every
	// responder on this subject, so a responder that cannot verify the grant (a
	// stale process with an old secret, or another tenant's responder) must not
	// answer — otherwise its rejection could win the reply race against the
	// legitimate responder and surface a spurious "unauthorized". A genuinely bad
	// grant simply gets no reply → the gateway times out ("session not found").
	grant, err := VerifyGrant(req.Grant, r.GrantSecret, time.Now())
	if err != nil {
		log.Printf("[webterm] ignoring request with unverifiable grant (viewer=%s): %v", req.ViewerID, err)
		return
	}
	// The grant is scoped to a vignoble; ignore grants for a different namespace
	// (a responder must only serve its own vignoble). Silent, same reasoning as an
	// unverifiable grant.
	if grant.Vignoble != "" && grant.Vignoble != r.Vignoble {
		log.Printf("[webterm] ignoring request for foreign vignoble %q (ours=%q)", grant.Vignoble, r.Vignoble)
		return
	}
	if req.ViewerID == "" {
		r.reply(m, false, "missing viewer id")
		return
	}

	base, _ := parseTarget(grant.Target)
	if !r.hasSession(base) {
		r.reply(m, false, "session not found")
		return
	}
	if int(atomic.LoadInt32(&r.active)) >= r.maxViewers() {
		r.reply(m, false, "too many viewers")
		return
	}

	// Accept, then start streaming.
	r.reply(m, true, "")
	atomic.AddInt32(&r.active, 1)
	go func() {
		defer atomic.AddInt32(&r.active, -1)
		r.serveViewer(ctx, req, grant)
	}()
}

// parseTarget splits a tmux target into a sanitized session + optional window.
// The two parts are sanitized separately so a legitimate `session:window` target
// (control-room windows) survives — SanitizeName maps ':' to '-'.
func parseTarget(t string) (base, window string) {
	b, w, ok := strings.Cut(t, ":")
	base = session.SanitizeName(b)
	if ok {
		window = session.SanitizeName(w)
	}
	return base, window
}

// handleList answers a control-room enumeration request: the vignoble's tmux
// sessions + the conductor's windows. Grant-verified; silent on failure (same as
// handleRequest) so a stale/foreign responder can't win the reply race.
func (r *Responder) handleList(m *nats.Msg) {
	if m.Reply == "" {
		return
	}
	var req ListReq
	if json.Unmarshal(m.Data, &req) != nil {
		return
	}
	grant, err := VerifyGrant(req.Grant, r.GrantSecret, time.Now())
	if err != nil {
		log.Printf("[webterm] ignoring list request with unverifiable grant: %v", err)
		return
	}
	if grant.Vignoble != "" && grant.Vignoble != r.Vignoble {
		return
	}
	data, _ := json.Marshal(ListReply{
		OK:       true,
		Sessions: r.tmuxSessions(),
		Windows:  r.tmuxConductorWindows(),
	})
	_ = m.Respond(data)
}

func (r *Responder) tmuxSessions() []string {
	out, err := exec.Command("tmux", "-L", r.socket(), "list-sessions", "-F", "#{session_name}").Output()
	if err != nil {
		return nil
	}
	var sessions []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line != "" {
			sessions = append(sessions, line)
		}
	}
	return sessions
}

func (r *Responder) tmuxConductorWindows() []WinInfo {
	out, err := exec.Command("tmux", "-L", r.socket(), "list-windows", "-t", "conductor", "-F", "#{window_index}:#{window_name}").Output()
	if err != nil {
		return nil // no conductor session on this host — fine
	}
	var wins []WinInfo
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line == "" {
			continue
		}
		idx, name, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		n, _ := strconv.Atoi(idx)
		wins = append(wins, WinInfo{Index: n, Name: name})
	}
	return wins
}

func (r *Responder) hasSession(target string) bool {
	err := exec.Command("tmux", "-L", r.socket(), "has-session", "-t", target).Run()
	return err == nil
}

// serveViewer attaches to the target and bridges the PTY to the viewer's NATS
// subjects until the browser disconnects, the session goes idle, or the tmux
// target exits. Read-only (attach -r) unless the grant is ModeRW ("steer"). For a
// window target (session:window) it attaches via a per-viewer grouped session so
// navigating windows doesn't move the real operator's active window.
func (r *Responder) serveViewer(ctx context.Context, req ReqMsg, grant Grant) {
	cols, rows := req.Cols, req.Rows
	if cols <= 0 {
		cols = 80
	}
	if rows <= 0 {
		rows = 24
	}
	base, window := parseTarget(grant.Target)
	writable := grant.Mode == ModeRW

	// For plain sessions (no window), we drive the tmux window size directly:
	// tmux attach -r sets ignore-size on the client, so the window stays at
	// 80×24 even though the PTY is the right size. We work around this by
	// issuing an explicit resize-window after attach and on every CtlResize.
	plainSession := window == ""

	// Grouped session for a specific window: shares the base session's windows but
	// has its own current-window, so a viewer can pin a window (régisseur/maître)
	// without yanking the operator. Torn down with the viewer.
	attachTarget := base
	groupName := ""
	if window != "" {
		groupName = session.SanitizeName("wt-" + req.ViewerID)
		if err := exec.Command("tmux", "-L", r.socket(), "new-session", "-d", "-s", groupName, "-t", base).Run(); err == nil {
			// Pin window-size to "manual" so the viewer's terminal dimensions never
			// propagate to the group and resize the operator's client.
			_ = exec.Command("tmux", "-L", r.socket(), "set-option", "-t", groupName, "window-size", "manual").Run()
			_ = exec.Command("tmux", "-L", r.socket(), "select-window", "-t", groupName+":"+window).Run()
			attachTarget = groupName
		} else {
			groupName = "" // grouping failed → fall back to attaching the base session
		}
	}

	// attach -r = read-only; without -r keystrokes reach the session (steer).
	args := []string{"-L", r.socket(), "attach"}
	if !writable {
		args = append(args, "-r")
	}
	args = append(args, "-t", attachTarget)
	cmd := exec.Command("tmux", args...)
	ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if err != nil {
		if groupName != "" {
			_ = exec.Command("tmux", "-L", r.socket(), "kill-session", "-t", groupName).Run()
		}
		r.publishEnded(req.ViewerID, "attach failed")
		return
	}

	viewerCtx, cancel := context.WithCancel(ctx)
	var teardownOnce sync.Once
	teardown := func() {
		teardownOnce.Do(func() {
			cancel()
			_ = ptmx.Close()
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
			if groupName != "" {
				_ = exec.Command("tmux", "-L", r.socket(), "kill-session", "-t", groupName).Run()
			}
		})
	}
	defer teardown()

	// tmuxResizePlain drives the window to the viewer's size for plain sessions.
	// attach -r sets ignore-size on the client, so we must push the size ourselves.
	tmuxResizePlain := func(c, rw int) {
		if !plainSession || c <= 0 || rw <= 0 {
			return
		}
		_ = exec.Command("tmux", "-L", r.socket(), "resize-window", "-t", attachTarget,
			"-x", strconv.Itoa(c), "-y", strconv.Itoa(rw)).Run()
	}

	// Pin window-size to manual on plain sessions so resize-window is authoritative
	// and isn't overridden by another client joining later.
	if plainSession {
		_ = exec.Command("tmux", "-L", r.socket(), "set-option", "-t", attachTarget, "window-size", "manual").Run()
		tmuxResizePlain(cols, rows)
	}

	// Idle tracking: reset on each gateway heartbeat/ctl message.
	lastSeen := time.Now()
	var idleMu sync.Mutex
	touch := func() { idleMu.Lock(); lastSeen = time.Now(); idleMu.Unlock() }

	// Control channel (gateway → responder): resize / close / heartbeat.
	ctlSub, err := r.NC.Subscribe(CtlSubject(r.Vignoble, req.ViewerID), func(m *nats.Msg) {
		touch()
		var c CtlMsg
		if json.Unmarshal(m.Data, &c) != nil {
			return
		}
		switch c.Type {
		case CtlResize:
			if c.Cols > 0 && c.Rows > 0 {
				_ = pty.Setsize(ptmx, &pty.Winsize{Cols: uint16(c.Cols), Rows: uint16(c.Rows)})
				tmuxResizePlain(c.Cols, c.Rows)
			}
		case CtlClose:
			teardown()
		case CtlInterrupt:
			r.handleInterrupt(base, grant, req.ViewerID)
		}
	})
	if err != nil {
		return
	}
	defer ctlSub.Unsubscribe()

	// Input channel (gateway → responder): keystrokes, only for a writable grant.
	// The gateway only forwards input when it minted a ModeRW grant, and we honor
	// it only when this grant is ModeRW — single-writer steer, gated both ends.
	if writable {
		inSub, ierr := r.NC.Subscribe(InSubject(r.Vignoble, req.ViewerID), func(m *nats.Msg) {
			touch()
			_, _ = ptmx.Write(m.Data)
		})
		if ierr == nil {
			defer inSub.Unsubscribe()
		}
	}

	// Idle watchdog.
	go func() {
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-viewerCtx.Done():
				return
			case <-t.C:
				idleMu.Lock()
				idle := time.Since(lastSeen)
				idleMu.Unlock()
				if idle > r.idleTimeout() {
					r.publishEnded(req.ViewerID, "idle timeout")
					teardown()
					return
				}
			}
		}
	}()

	outSubject := OutSubject(r.Vignoble, req.ViewerID)
	r.pump(viewerCtx, ptmx, outSubject)

	// pump returned → PTY EOF (tmux target exited or attach ended).
	r.publishEnded(req.ViewerID, "session ended")
}

// pump reads PTY output and publishes it with coalescing, a bounded buffer, and
// a per-second rate cap.
func (r *Responder) pump(ctx context.Context, ptmx interface{ Read([]byte) (int, error) }, outSubject string) {
	var mu sync.Mutex
	pending := make([]byte, 0, maxBufBytes)
	dropped := false

	// Reader goroutine appends into the bounded buffer.
	readErr := make(chan struct{})
	go func() {
		buf := make([]byte, 32*1024)
		for {
			n, err := ptmx.Read(buf)
			if n > 0 {
				mu.Lock()
				pending = append(pending, buf[:n]...)
				if len(pending) > maxBufBytes {
					// Drop oldest to keep the buffer bounded; mark once.
					over := len(pending) - maxBufBytes
					pending = pending[over:]
					dropped = true
				}
				mu.Unlock()
			}
			if err != nil {
				close(readErr)
				return
			}
		}
	}()

	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	ticksPerSec := int(time.Second / flushInterval)
	if ticksPerSec < 1 {
		ticksPerSec = 1
	}
	maxPerTick := rateCapPerSec / ticksPerSec

	flush := func() {
		mu.Lock()
		if dropped {
			pending = append([]byte(throttleMark), pending...)
			dropped = false
		}
		take := pending
		if len(take) > maxPerTick {
			take = pending[:maxPerTick]
			pending = pending[maxPerTick:]
		} else {
			pending = pending[:0]
		}
		mu.Unlock()
		for len(take) > 0 {
			chunk := take
			if len(chunk) > maxMsgBytes {
				chunk = take[:maxMsgBytes]
			}
			_ = r.NC.Publish(outSubject, chunk)
			take = take[len(chunk):]
		}
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-readErr:
			flush() // drain remaining output
			return
		case <-ticker.C:
			flush()
		}
	}
}

func (r *Responder) publishEnded(viewerID, reason string) {
	data, _ := json.Marshal(EvtMsg{Type: EvtEnded, Reason: reason})
	_ = r.NC.Publish(EvtSubject(r.Vignoble, viewerID), data)
}

// handleInterrupt sends the interrupt signal to the agent attached to base
// (the tmux session name). It first tries to publish to the agent's NATS
// interrupt subject (resolved via the pinard-agents KV); on failure it falls
// back to tmux send-keys so the signal still reaches the session.
func (r *Responder) handleInterrupt(base string, grant Grant, viewerID string) {
	log.Printf("[webterm-audit] action=interrupt viewer=%s vignoble=%s target=%s mode=%s",
		viewerID, r.Vignoble, grant.Target, grant.Mode)

	subject := r.resolveInterruptSubject(base)
	if subject != "" {
		_ = r.NC.Publish(subject, []byte(`{}`))
		return
	}
	// KV lookup failed (nil KV or session not found). Log and no-op: the designed
	// interrupt mechanism is a NATS publish to the agent's .interrupt subject (same
	// as the conductor's interrupt_worker). A destructive tmux send-keys C-c could
	// terminate the agent process rather than cleanly cancelling the current turn,
	// which is unsafe for read-only capsule viewers. With KV wired this path should
	// not be reached for live sessions.
	log.Printf("[webterm] interrupt: could not resolve NATS subject for session %q (KV unavailable or session not found) — no-op", base)
}
