package webterm

import (
	"context"
	"fmt"
	"log"
	"os/exec"
	"sync"
	"time"

	"github.com/creack/pty"
	"github.com/nats-io/nats.go"
)

// AgentPump attaches to a local tmux session in read-only mode and continuously
// publishes its PTY output to PtyOutSubject. It is started on-demand by
// `aoc attach` when the target session is on the local tmux server.
//
// The pump uses the same rate-limited coalescing logic as Responder. Multiple
// concurrent subscribers on PtyOutSubject are all served by one pump instance —
// NATS fan-out handles distribution.
type AgentPump struct {
	NC       *nats.Conn
	Vignoble string
	Socket   string // tmux socket; defaults to "pinard-<vignoble>"
	Session  string // tmux session name (sanitized)
	AgentID  string // key used in PtyOutSubject; defaults to Session

	// Cols/Rows for the initial attach PTY size.
	Cols int
	Rows int
}

func (p *AgentPump) socket() string {
	if p.Socket != "" {
		return p.Socket
	}
	return "pinard-" + p.Vignoble
}

func (p *AgentPump) agentID() string {
	if p.AgentID != "" {
		return p.AgentID
	}
	return p.Session
}

func (p *AgentPump) cols() int {
	if p.Cols > 0 {
		return p.Cols
	}
	return 220
}

func (p *AgentPump) rows() int {
	if p.Rows > 0 {
		return p.Rows
	}
	return 50
}

// Run attaches to the tmux session and publishes output until ctx is cancelled
// or the session exits. Blocks until done.
func (p *AgentPump) Run(ctx context.Context) error {
	if p.NC == nil {
		return fmt.Errorf("agent pump: nil NATS connection")
	}
	if p.Session == "" {
		return fmt.Errorf("agent pump: empty session name")
	}

	args := []string{"-L", p.socket(), "attach", "-r", "-t", p.Session}
	cmd := exec.Command("tmux", args...)
	ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{
		Cols: uint16(p.cols()),
		Rows: uint16(p.rows()),
	})
	if err != nil {
		return fmt.Errorf("agent pump attach %q: %w", p.Session, err)
	}

	outSubject := PtyOutSubject(p.Vignoble, p.agentID())
	log.Printf("[agent-pump] publishing %s → %s", p.Session, outSubject)

	pumpCtx, cancel := context.WithCancel(ctx)
	var once sync.Once
	teardown := func() {
		once.Do(func() {
			cancel()
			_ = ptmx.Close()
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		})
	}
	defer teardown()

	done := make(chan struct{})
	go func() {
		defer close(done)
		pumpBytes(pumpCtx, ptmx, p.NC, outSubject)
	}()

	select {
	case <-pumpCtx.Done():
		return pumpCtx.Err()
	case <-done:
		return fmt.Errorf("agent pump: session %q ended", p.Session)
	}
}

// pumpBytes reads from ptmx and publishes to outSubject with coalescing,
// a bounded buffer, and a per-second rate cap. Returns when ctx is done
// or ptmx returns an error (EOF / session exit).
func pumpBytes(ctx context.Context, ptmx interface{ Read([]byte) (int, error) }, nc *nats.Conn, outSubject string) {
	var mu sync.Mutex
	pending := make([]byte, 0, maxBufBytes)
	dropped := false

	readErr := make(chan struct{})
	go func() {
		buf := make([]byte, 32*1024)
		for {
			n, err := ptmx.Read(buf)
			if n > 0 {
				mu.Lock()
				pending = append(pending, buf[:n]...)
				if len(pending) > maxBufBytes {
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
			_ = nc.Publish(outSubject, chunk)
			take = take[len(chunk):]
		}
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-readErr:
			flush()
			return
		case <-ticker.C:
			flush()
		}
	}
}
