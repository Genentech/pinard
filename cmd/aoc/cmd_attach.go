package main

import (
	"context"
	"encoding/hex"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"crypto/rand"
	"encoding/json"
	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/webterm"
	term "github.com/charmbracelet/x/term"
	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

var attachCmd = &cobra.Command{
	Use:   "attach [session]",
	Short: "Stream a vendangeur's terminal output over NATS (read-only)",
	Long: "Subscribes to a vendangeur session's live PTY output and renders it to the\n" +
		"local terminal. The session is resolved from the pinard-agents KV by name,\n" +
		"agentId, or runId. Uses the grant-gated responder protocol (same as the\n" +
		"web gateway) — requires webterm.grant_secret in credentials.\n\n" +
		"Press Ctrl+C to detach.",
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if len(args) == 0 {
			return fmt.Errorf("session name is required")
		}
		token := args[0]
		timeout, _ := cmd.Flags().GetDuration("timeout")
		steer, _ := cmd.Flags().GetBool("steer")

		creds, err := config.LoadCredentials()
		if err != nil {
			return fmt.Errorf("credentials: %w", err)
		}
		grantSecret := creds.WebtermGrantSecret()
		if len(grantSecret) == 0 {
			return fmt.Errorf("attach requires webterm.grant_secret in credentials")
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
		sessionName, _ := resolveAttachTarget(kv, token)

		// Mint a short-lived grant (RO by default; RW only with --steer).
		mode := webterm.ModeRO
		if steer {
			mode = webterm.ModeRW
		}
		exp := time.Now().Add(5 * time.Minute).Unix()
		grant, err := webterm.SignGrant(webterm.Grant{
			Vignoble: vignoble,
			Target:   sessionName,
			Mode:     mode,
			Exp:      exp,
		}, grantSecret)
		if err != nil {
			return fmt.Errorf("sign grant: %w", err)
		}

		// Generate a unique viewer ID for this attach session.
		var rawID [8]byte
		if _, err := rand.Read(rawID[:]); err != nil {
			return fmt.Errorf("generate viewer id: %w", err)
		}
		viewerID := "cli-" + hex.EncodeToString(rawID[:])

		cols, rows := 220, 50
		if w, h, serr := term.GetSize(os.Stdout.Fd()); serr == nil {
			cols, rows = w, h
		}

		// Send a ReqMsg to the responder and wait for acceptance.
		reqData, _ := json.Marshal(webterm.ReqMsg{
			Grant:    grant,
			ViewerID: viewerID,
			Cols:     cols,
			Rows:     rows,
		})
		msg, err := nc.Conn().Request(webterm.ReqSubject(vignoble), reqData, 5*time.Second)
		if err != nil {
			return fmt.Errorf("no responder answered (is the daemon or webterm-responder running?): %w", err)
		}
		var reply webterm.ReqReply
		if err := json.Unmarshal(msg.Data, &reply); err != nil {
			return fmt.Errorf("bad responder reply: %w", err)
		}
		if !reply.OK {
			return fmt.Errorf("responder rejected attach: %s", reply.Reason)
		}

		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()

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

		// Subscribe to the per-viewer output subject.
		outSubject := webterm.OutSubject(vignoble, viewerID)
		lastMsg := time.Now()
		outSub, err := nc.Conn().Subscribe(outSubject, func(m *nats.Msg) {
			lastMsg = time.Now()
			_, _ = os.Stdout.Write(m.Data)
		})
		if err != nil {
			return fmt.Errorf("subscribe %s: %w", outSubject, err)
		}
		defer outSub.Unsubscribe() //nolint:errcheck

		// Subscribe to session-ended events.
		evtSub, err := nc.Conn().Subscribe(webterm.EvtSubject(vignoble, viewerID), func(m *nats.Msg) {
			var evt webterm.EvtMsg
			if json.Unmarshal(m.Data, &evt) == nil && evt.Type == webterm.EvtEnded {
				stop()
			}
		})
		if err != nil {
			return fmt.Errorf("subscribe evt: %w", err)
		}
		defer evtSub.Unsubscribe() //nolint:errcheck

		// For steer mode: forward stdin to the responder's input subject.
		if steer {
			go func() {
				inSubject := webterm.InSubject(vignoble, viewerID)
				buf := make([]byte, 4096)
				for {
					n, err := os.Stdin.Read(buf)
					if n > 0 {
						_ = nc.Conn().Publish(inSubject, buf[:n])
					}
					if err != nil {
						return
					}
					select {
					case <-ctx.Done():
						return
					default:
					}
				}
			}()
		}

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

		// Heartbeat so the responder's idle watchdog stays alive.
		hbTick := time.NewTicker(30 * time.Second)
		defer hbTick.Stop()

		ctlSubject := webterm.CtlSubject(vignoble, viewerID)

		for {
			select {
			case <-ctx.Done():
				restore()
				// Send close so the responder tears down immediately.
				data, _ := json.Marshal(webterm.CtlMsg{Type: webterm.CtlClose})
				_ = nc.Conn().Publish(ctlSubject, data)
				fmt.Fprintf(os.Stderr, "\r\n\x1b[33m[aoc attach] detached\x1b[0m\r\n")
				return nil
			case sig := <-sigwinch:
				_ = sig
				if w, h, serr := term.GetSize(os.Stdout.Fd()); serr == nil {
					data, _ := json.Marshal(webterm.CtlMsg{Type: webterm.CtlResize, Cols: w, Rows: h})
					_ = nc.Conn().Publish(ctlSubject, data)
				}
			case <-hbTick.C:
				data, _ := json.Marshal(webterm.CtlMsg{Type: webterm.CtlHeartbeat})
				_ = nc.Conn().Publish(ctlSubject, data)
			case <-idleTick:
				if timeout > 0 && time.Since(lastMsg) >= timeout {
					restore()
					data, _ := json.Marshal(webterm.CtlMsg{Type: webterm.CtlClose})
					_ = nc.Conn().Publish(ctlSubject, data)
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

func init() {
	attachCmd.Flags().String("vignoble-name", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE or the resolved vignoble")
	attachCmd.Flags().Duration("timeout", 0, "Detach after this much idle time (0 = no timeout)")
	attachCmd.Flags().Bool("steer", false, "Open in read-write (steer) mode — requires ModeRW grant")
	rootCmd.AddCommand(attachCmd)
}
