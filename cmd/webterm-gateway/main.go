// Command webterm-gateway is the k8s-side web-terminal gateway. It serves the
// xterm.js frontend and bridges browser WebSockets to the host-side responder
// over NATS.
//
// Auth (Phase 2): when webterm.auth is configured (issuer + client_id), the
// gateway runs the Cognito OIDC login flow (Authorization Code + PKCE) itself,
// validates the ID token, and authorizes per target (operator → any; viewer →
// signed-link target). Without it, the signed link is the sole gate (Phase 1).
//
// Config comes from the standard pinard credentials (webterm.* section) and the
// vignoble name from NATS_VIGNOBLE / PINARD_VIGNOBLE_NAME. Listen address from
// WEBTERM_ADDR (default :8080).
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/webterm"
)

// splitCSV parses a comma-separated list, trimming spaces and dropping empties.
func splitCSV(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func main() {
	// serve-site mode: back the website chart from this same image (no creds/NATS).
	if len(os.Args) > 1 && os.Args[1] == "serve-site" {
		os.Exit(runServeSite(os.Args[2:]))
	}

	creds, err := config.LoadCredentials()
	if err != nil {
		log.Fatalf("load credentials: %v", err)
	}
	if !creds.WebtermEnabled() {
		log.Fatalf("webterm not configured: need webterm.base_url + link + grant secrets")
	}

	// Served vignobles: WEBTERM_VIGNOBLES (comma-separated) as an explicit allowlist,
	// falling back to the single NATS_VIGNOBLE / PINARD_VIGNOBLE_NAME. An empty set is
	// valid and preferred: the gateway then serves any vignoble present in the
	// pinard-vignobles KV (self-maintaining).
	vignobles := splitCSV(os.Getenv("WEBTERM_VIGNOBLES"))
	if len(vignobles) == 0 {
		single := os.Getenv("NATS_VIGNOBLE")
		if single == "" {
			single = os.Getenv("PINARD_VIGNOBLE_NAME")
		}
		if single != "" {
			vignobles = []string{single}
		}
	}

	nc := pnats.NewClient(creds)
	if err := nc.Connect(); err != nil {
		log.Fatalf("NATS connect: %v", err)
	}
	defer nc.Close()

	gw := &webterm.Gateway{
		NC:          nc.Conn(),
		Vignobles:   vignobles,
		LinkSecret:  creds.WebtermLinkSecret(),
		GrantSecret: creds.WebtermGrantSecret(),
		Owners:      &webterm.OwnerStore{KV: pnats.NewKV(nc)},
	}

	// Auth: enforce Cognito login when configured; otherwise Phase-1 signed-link.
	if creds.WebtermAuthEnabled() {
		if len(creds.WebtermCookieSecret()) == 0 {
			log.Fatalf("webterm.auth configured but cookie secret missing (set cookie_secret / cookie_secret_env)")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		auth, err := webterm.NewAuthenticator(ctx,
			creds.Webterm.Auth.Issuer,
			creds.Webterm.Auth.ClientID,
			creds.WebtermRedirectURL(),
			creds.WebtermScopes(),
			creds.WebtermCookieSecret(),
			creds.WebtermSessionTTL(),
			strings.HasPrefix(creds.WebtermBaseURL(), "https://"),
		)
		cancel()
		if err != nil {
			log.Fatalf("webterm auth init: %v", err)
		}
		gw.Auth = auth
		log.Printf("[webterm] Cognito auth ENABLED (issuer=%s)", creds.Webterm.Auth.Issuer)
	} else {
		log.Printf("[webterm] auth DISABLED — signed link is the sole gate (Phase 1)")
	}

	addr := os.Getenv("WEBTERM_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	served := "KV-derived (any vignoble with a published owner)"
	if len(vignobles) > 0 {
		served = strings.Join(vignobles, ",")
	}
	log.Printf("[webterm] gateway listening on %s (vignobles=%s)", addr, served)
	if err := http.ListenAndServe(addr, gw.Handler()); err != nil {
		log.Fatalf("http server: %v", err)
	}
}
