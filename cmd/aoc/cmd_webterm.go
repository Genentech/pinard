package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/webterm"
	"github.com/creack/pty"
	"github.com/spf13/cobra"
)

// resolveVignobleName returns the vignoble name for webterm subjects, from
// --vignoble-name, NATS_VIGNOBLE, or a resolvable vignoble directory (in that
// order). Standalone/HPC hosts have no vignoble dir, so the flag/env wins.
func resolveVignobleName(cmd *cobra.Command) string {
	if v, _ := cmd.Flags().GetString("vignoble-name"); v != "" {
		return v
	}
	if v := os.Getenv("NATS_VIGNOBLE"); v != "" {
		return v
	}
	if vb, err := config.ResolveVignoble(); err == nil {
		return vb.Name
	}
	return ""
}

var webtermResponderCmd = &cobra.Command{
	Use:   "webterm-responder",
	Short: "Run the web-terminal responder (streams local tmux targets over NATS)",
	Long: "Serves read-only browser terminal views for local tmux sessions on this host.\n" +
		"The pinard host runs this in-process via the daemon; use this command on\n" +
		"standalone/HPC worker hosts. Requires webterm.grant_secret in credentials.",
	RunE: func(cmd *cobra.Command, args []string) error {
		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		if !creds.WebtermResponderEnabled() {
			return fmt.Errorf("webterm responder not configured: set webterm.grant_secret (or grant_secret_env)")
		}
		vignoble := resolveVignobleName(cmd)
		if vignoble == "" {
			return fmt.Errorf("could not resolve vignoble name (use --vignoble-name or set NATS_VIGNOBLE)")
		}

		nc := pnats.NewClient(creds)
		if err := nc.Connect(); err != nil {
			return err
		}
		defer nc.Close()

		// Publish this vignoble's owner for gateway operator authorization (D7).
		if err := webterm.PublishOwner(pnats.NewKV(nc), vignoble, creds.WebtermOwner()); err != nil {
			log.Printf("[webterm] publish owner failed: %v", err)
		}

		resp := &webterm.Responder{
			NC:          nc.Conn(),
			Vignoble:    vignoble,
			GrantSecret: creds.WebtermGrantSecret(),
			MaxViewers:  creds.WebtermMaxViewers(),
			IdleTimeout: creds.WebtermIdleTimeout(),
			KV:          pnats.NewKV(nc),
		}

		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		log.Printf("[webterm] responder starting (vignoble=%s)", vignoble)
		return resp.Run(ctx)
	},
}

var webtermLinkCmd = &cobra.Command{
	Use:   "webterm-link",
	Short: "Print a read-only terminal link for a tmux target (unsigned when SSO auth is on, else signed+expiring)",
	RunE: func(cmd *cobra.Command, args []string) error {
		target, _ := cmd.Flags().GetString("target")
		if target == "" {
			return fmt.Errorf("--target is required")
		}
		auto, _ := cmd.Flags().GetBool("auto")
		creds, err := config.LoadCredentials()
		// --auto: silently print nothing (exit 0) when link posting is not enabled,
		// so automated callers (e.g. the babysitter "Vendangeur attached" comment)
		// can invoke unconditionally and append a link only when one exists.
		if auto {
			if err != nil || !creds.WebtermEnabled() || !creds.WebtermPostLinks() {
				return nil
			}
		} else {
			if err != nil {
				return err
			}
			if !creds.WebtermEnabled() {
				return fmt.Errorf("webterm not configured: need webterm.base_url + link_secret + grant_secret")
			}
		}
		vignoble := resolveVignobleName(cmd)
		if vignoble == "" {
			if auto {
				return nil
			}
			return fmt.Errorf("could not resolve vignoble (use --vignoble-name or set NATS_VIGNOBLE)")
		}
		// Mirror `aoc track_mr`: with Cognito SSO enabled, emit an UNSIGNED link (no
		// bearer in the URL; the gateway grants only SSO-authenticated operators).
		// Without auth, fall back to a signed, expiring link.
		if creds.WebtermAuthEnabled() {
			fmt.Println(webterm.BuildUnsignedLink(creds.WebtermBaseURL(), vignoble, target))
			return nil
		}
		ttl := creds.WebtermLinkTTL()
		if v, _ := cmd.Flags().GetDuration("ttl"); v > 0 {
			ttl = v
		}
		exp := time.Now().Add(ttl)
		fmt.Println(webterm.BuildLink(creds.WebtermBaseURL(), vignoble, target, exp, creds.WebtermLinkSecret()))
		return nil
	},
}

// webtermWorkerResponderCmd is the worker-self-served responder: it bridges the
// worker's own pi PTY directly over NATS using the same grant-gated protocol as
// the host Responder (TmuxBackend), without requiring tmux. This is the
// daemon-less / HPC / Singularity --containall path.
//
// The command reads from the PTY master fd number passed via --pty-fd (the
// caller — bin/pinard --worker — opens a PTY pair and passes the master fd).
// It subscribes to ReqSubject, verifies grants, and serves each viewer via
// per-viewer OutSubject/InSubject/CtlSubject/EvtSubject.
var webtermWorkerResponderCmd = &cobra.Command{
	Use:   "webterm-worker-responder",
	Short: "Run the grant-gated worker PTY responder (daemon-less / HPC path)",
	Long: "Serves the worker's own PTY over NATS using the same grant-gated protocol as\n" +
		"webterm-responder, but without tmux. Called by bin/pinard --worker before\n" +
		"exec'ing pi on daemon-less / HPC / Singularity hosts.\n\n" +
		"Requires --session-name and --pty-fd; webterm.grant_secret in credentials.",
	RunE: func(cmd *cobra.Command, args []string) error {
		ptyFD, _ := cmd.Flags().GetInt("pty-fd")
		sessionName, _ := cmd.Flags().GetString("session-name")
		if ptyFD < 0 {
			return fmt.Errorf("--pty-fd is required")
		}
		if sessionName == "" {
			return fmt.Errorf("--session-name is required")
		}

		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		if !creds.WebtermResponderEnabled() {
			// Not configured — silently exit. The worker still runs without a
			// responder; the feature is opt-in via grant_secret.
			return nil
		}
		vignoble := resolveVignobleName(cmd)
		if vignoble == "" {
			return fmt.Errorf("could not resolve vignoble name (use --vignoble-name or set NATS_VIGNOBLE)")
		}

		ptyFile := os.NewFile(uintptr(ptyFD), "pty-master")
		if ptyFile == nil {
			return fmt.Errorf("could not open pty fd %d", ptyFD)
		}

		nc := pnats.NewClient(creds)
		if err := nc.Connect(); err != nil {
			return err
		}
		defer nc.Close()

		backend := &webterm.ProcessBackend{
			Target: sessionName,
			PTY:    ptyFile,
			Setsize: func(cols, rows int) {
				_ = pty.Setsize(ptyFile, &pty.Winsize{
					Cols: uint16(cols),
					Rows: uint16(rows),
				})
			},
		}

		resp := &webterm.Responder{
			NC:          nc.Conn(),
			Vignoble:    vignoble,
			GrantSecret: creds.WebtermGrantSecret(),
			MaxViewers:  creds.WebtermMaxViewers(),
			IdleTimeout: creds.WebtermIdleTimeout(),
			Backend:     backend,
		}

		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		log.Printf("[webterm] worker responder starting (vignoble=%s session=%s)", vignoble, sessionName)
		return resp.Run(ctx)
	},
}

func init() {
	webtermResponderCmd.Flags().String("vignoble-name", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE or the resolved vignoble")
	rootCmd.AddCommand(webtermResponderCmd)

	webtermWorkerResponderCmd.Flags().String("vignoble-name", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE or the resolved vignoble")
	webtermWorkerResponderCmd.Flags().Int("pty-fd", -1, "PTY master file descriptor (opened by the caller)")
	webtermWorkerResponderCmd.Flags().String("session-name", "", "Session name this responder answers for")
	rootCmd.AddCommand(webtermWorkerResponderCmd)

	webtermLinkCmd.Flags().String("target", "", "tmux target (session name)")
	webtermLinkCmd.Flags().String("vignoble-name", "", "Vignoble name; defaults to NATS_VIGNOBLE or the resolved vignoble")
	webtermLinkCmd.Flags().Duration("ttl", 0, "link lifetime (overrides webterm.link_ttl)")
	webtermLinkCmd.Flags().Bool("auto", false, "Silently print nothing (exit 0) when webterm/post_links is disabled; for automated callers")
	rootCmd.AddCommand(webtermLinkCmd)
}
