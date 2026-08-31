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

func init() {
	webtermResponderCmd.Flags().String("vignoble-name", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE or the resolved vignoble")
	rootCmd.AddCommand(webtermResponderCmd)

	webtermLinkCmd.Flags().String("target", "", "tmux target (session name)")
	webtermLinkCmd.Flags().String("vignoble-name", "", "Vignoble name; defaults to NATS_VIGNOBLE or the resolved vignoble")
	webtermLinkCmd.Flags().Duration("ttl", 0, "link lifetime (overrides webterm.link_ttl)")
	webtermLinkCmd.Flags().Bool("auto", false, "Silently print nothing (exit 0) when webterm/post_links is disabled; for automated callers")
	rootCmd.AddCommand(webtermLinkCmd)
}
