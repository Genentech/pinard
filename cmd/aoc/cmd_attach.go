package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"

	term "github.com/charmbracelet/x/term"
	"github.com/nats-io/nats.go"
	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/webterm"
	"github.com/spf13/cobra"
)

var attachCmd = &cobra.Command{
	Use:   "attach [session]",
	Short: "Stream a vendangeur's terminal output over NATS (read-only)",
	Long: "Subscribes to a vendangeur session's live PTY output and renders it to the\n" +
		"local terminal. The session is resolved from the pinard-agents KV by name,\n" +
		"agentId, or runId. Authenticates with operator NATS credentials only — no\n" +
		"grant or SSO is required for read-only access.\n\n" +
		"For sessions running on this host a local PTY pump is started automatically.\n" +
		"Press Ctrl+C to detach.",
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if len(args) == 0 {
			return fmt.Errorf("session name is required")
		}
		token := args[0]
		timeout, _ := cmd.Flags().GetDuration("timeout")

		creds, err := config.LoadCredentials()
		if err != nil {
			return fmt.Errorf("credentials: %w", err)
		}

		vignoble := resolveVignobleName(cmd)
		if vignoble == "" {
			return fmt.Errorf("could not resolve vignoble name (use --vignoble-name or set NATS_VIGNOBLE)")
		}

		nc := pnats.NewClient(creds)
		if err := nc.Connect(); err != nil {
			return fmt.Errorf("NATS: %w", err)
		}
		defer nc.Close()

		kv := pnats.NewKV(nc)
		sessionName, agentID := resolveAttachTarget(kv, token)
		tmuxSocket := "pinard-" + vignoble

		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()

		// For local sessions: start an in-process PTY pump (tmux → NATS).
		local := isLocalSession(tmuxSocket, sessionName)
		if local {
			pump := &webterm.AgentPump{
				NC:       nc.Conn(),
				Vignoble: vignoble,
				Socket:   tmuxSocket,
				Session:  sessionName,
				AgentID:  agentID,
			}
			if w, h, serr := term.GetSize(os.Stdout.Fd()); serr == nil {
				pump.Cols = w
				pump.Rows = h
			}
			go func() {
				_ = pump.Run(ctx)
			}()
			// Brief settle so the pump can start publishing before we subscribe.
			time.Sleep(50 * time.Millisecond)
		}

		outSubject := webterm.PtyOutSubject(vignoble, agentID)

		// Put the local terminal into raw mode so escape sequences render correctly.
		var oldState *term.State
		if term.IsTerminal(os.Stdin.Fd()) {
			oldState, err = term.MakeRaw(os.Stdin.Fd())
			if err != nil {
				fmt.Fprintf(os.Stderr, "warning: could not set raw mode: %v\n", err)
			}
		}
		restore := func() {
			if oldState != nil {
				_ = term.Restore(os.Stdin.Fd(), oldState)
				oldState = nil
			}
		}
		defer restore()

		lastMsg := time.Now()
		sub, err := nc.Conn().Subscribe(outSubject, func(msg *nats.Msg) {
			lastMsg = time.Now()
			_, _ = os.Stdout.Write(msg.Data)
		})
		if err != nil {
			return fmt.Errorf("subscribe %s: %w", outSubject, err)
		}
		defer sub.Unsubscribe() //nolint:errcheck

		fmt.Fprintf(os.Stderr, "\r\n\x1b[33m[aoc attach] watching %s (Ctrl+C to detach)\x1b[0m\r\n", sessionName)

		// Handle SIGWINCH (terminal resize).
		sigwinch := make(chan os.Signal, 1)
		signal.Notify(sigwinch, syscall.SIGWINCH)
		defer signal.Stop(sigwinch)

		// Idle timeout ticker (only when --timeout > 0).
		var idleTick <-chan time.Time
		if timeout > 0 {
			t := time.NewTicker(5 * time.Second)
			defer t.Stop()
			idleTick = t.C
		}

		for {
			select {
			case <-ctx.Done():
				restore()
				fmt.Fprintf(os.Stderr, "\r\n\x1b[33m[aoc attach] detached\x1b[0m\r\n")
				return nil
			case <-sigwinch:
				// No-op: the pump auto-attaches with a large default size.
				// Resize propagation is a follow-up.
			case <-idleTick:
				if timeout > 0 && time.Since(lastMsg) >= timeout {
					restore()
					fmt.Fprintf(os.Stderr, "\r\n\x1b[33m[aoc attach] idle timeout\x1b[0m\r\n")
					return nil
				}
			}
		}
	},
}

// resolveAttachTarget resolves a raw token (session name, agentId, or runId)
// to a (sessionName, agentID) pair using the pinard-agents KV. Falls back to
// using the token as both when no record is found.
func resolveAttachTarget(kv *pnats.KV, token string) (sessionName, agentID string) {
	rec := resolveAgentRecord(kv, token)
	if rec == nil {
		s := session.SanitizeName(token)
		return s, s
	}
	name, _ := rec["name"].(string)
	aid, _ := rec["agentId"].(string)
	if name == "" {
		name = token
	}
	if aid == "" {
		aid = name
	}
	return session.SanitizeName(name), aid
}

// isLocalSession returns true when the named session exists on the given tmux socket.
func isLocalSession(socket, name string) bool {
	return exec.Command("tmux", "-L", socket, "has-session", "-t", name).Run() == nil
}

func init() {
	attachCmd.Flags().String("vignoble-name", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE or the resolved vignoble")
	attachCmd.Flags().Duration("timeout", 0, "Detach after this much idle time (0 = no timeout)")
	rootCmd.AddCommand(attachCmd)
}
