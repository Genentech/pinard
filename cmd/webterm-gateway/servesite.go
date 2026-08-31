package main

// serve-site mode: serve a static directory (the pre-built Hugo site) over HTTP.
//
// This lets the single pinard image back BOTH the website chart and the
// webterm-gateway chart, selected by `command` — no nginx, no separate image.
// It needs none of the webterm/NATS credentials the gateway requires, so it
// short-circuits main() before credential loading.
//
//   webterm-gateway serve-site [--dir /srv/site] [--addr :80]

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// runServeSite serves static files from a directory. Returns the process exit code.
func runServeSite(args []string) int {
	fs := flag.NewFlagSet("serve-site", flag.ContinueOnError)
	dir := fs.String("dir", envOr("PINARD_SITE_DIR", "/srv/site"), "directory of static files to serve")
	addr := fs.String("addr", envOr("PINARD_SITE_ADDR", ":80"), "listen address")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	root, err := filepath.Abs(*dir)
	if err != nil {
		log.Printf("serve-site: %v", err)
		return 1
	}
	if st, err := os.Stat(root); err != nil || !st.IsDir() {
		log.Printf("serve-site: %q is not a directory", root)
		return 1
	}

	fileServer := http.FileServer(http.Dir(root))
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		// Serve the requested file; fall back to 404.html for unknown paths so
		// Hugo's generated 404 page is shown (static-site friendly).
		clean := filepath.Clean(r.URL.Path)
		if clean == "/" || !fileExists(filepath.Join(root, clean)) && !strings.HasSuffix(clean, "/") && !fileExists(filepath.Join(root, clean, "index.html")) {
			if p := filepath.Join(root, clean); !fileExists(p) && !fileExists(filepath.Join(root, clean, "index.html")) && clean != "/" {
				if fileExists(filepath.Join(root, "404.html")) {
					w.WriteHeader(http.StatusNotFound)
					http.ServeFile(w, r, filepath.Join(root, "404.html"))
					return
				}
			}
		}
		fileServer.ServeHTTP(w, r)
	})

	srv := &http.Server{
		Addr:              *addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("serve-site: serving %s on %s", root, *addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Printf("serve-site: %v", err)
		return 1
	}
	return 0
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}
