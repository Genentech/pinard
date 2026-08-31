package watcher

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/engram"
)

// EngramServer supervises the single per-vignoble `engram serve` process owned
// by the daemon. It starts the serve on daemon startup, reconciles any stray
// process on the port, and restarts if the serve dies.
type EngramServer struct {
	// VignobleName is the stripped vignoble name (without "vignoble-" prefix).
	VignobleName string
	// VignoblePath is the absolute path to the vignoble directory.
	VignoblePath string

	port    int
	dataDir string
	bin     string
	proc    *os.Process
}

// init resolves the engram binary and port. Returns an error if engram is not found.
func (e *EngramServer) init() error {
	p, err := exec.LookPath("engram")
	if err != nil {
		return fmt.Errorf("engram binary not found: %w", err)
	}
	e.bin = p
	e.port = engram.PortForVignoble(e.VignobleName)
	e.dataDir = filepath.Join(e.VignoblePath, ".engram")
	return nil
}

// Start reconciles any existing process on the port, then ensures our serve is
// running. Safe to call multiple times; idempotent when our serve is already up.
func (e *EngramServer) Start() error {
	if err := e.init(); err != nil {
		return err
	}
	url := fmt.Sprintf("http://127.0.0.1:%d", e.port)

	// Check whether something is already listening on our port.
	if pid := pidOnPort(e.port); pid > 0 {
		dataDir, err := engramDataDirFromPID(pid)
		if err != nil || dataDir == "" {
			// Cannot determine — assume it may be ours; adopt it.
			log.Printf("[engram-serve] port %d in use by pid %d (cannot read environ) — adopting", e.port, pid)
			proc, _ := os.FindProcess(pid)
			e.proc = proc
			return nil
		}
		if filepath.Clean(dataDir) == filepath.Clean(e.dataDir) {
			// Our serve is already up — adopt it.
			log.Printf("[engram-serve] adopting existing serve pid %d on port %d", pid, e.port)
			proc, _ := os.FindProcess(pid)
			e.proc = proc
			return nil
		}
		// Foreign serve (different data dir) — kill and replace.
		log.Printf("[engram-serve] killing foreign serve pid %d on port %d (data dir: %s)", pid, e.port, dataDir)
		if proc, err := os.FindProcess(pid); err == nil {
			_ = proc.Kill()
			// Wait for port to free up.
			for i := 0; i < 20; i++ {
				time.Sleep(100 * time.Millisecond)
				if pidOnPort(e.port) == 0 {
					break
				}
			}
		}
	}

	return e.launch(url)
}

// launch starts a fresh `engram serve` process.
func (e *EngramServer) launch(healthURL string) error {
	os.MkdirAll(e.dataDir, 0755)

	cmd := exec.Command(e.bin, "serve", "--port", fmt.Sprintf("%d", e.port))
	cmd.Dir = e.VignoblePath
	cmd.Env = append(os.Environ(),
		"ENGRAM_DATA_DIR="+e.dataDir,
		fmt.Sprintf("ENGRAM_PORT=%d", e.port),
		// Prevent engram's update-check from hanging on startup.
		"GITHUB_TOKEN=x",
	)
	// Discard stdout/stderr — engram serve is chatty; errors surface via health checks.
	cmd.Stdout = nil
	cmd.Stderr = nil

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("engram serve: start: %w", err)
	}
	e.proc = cmd.Process
	log.Printf("[engram-serve] started pid %d on port %d (data dir: %s)", e.proc.Pid, e.port, e.dataDir)

	// Wait for the serve to become healthy (up to 5s).
	client := &http.Client{Timeout: 500 * time.Millisecond}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := client.Get(healthURL + "/health")
		if err == nil {
			resp.Body.Close()
			log.Printf("[engram-serve] healthy on port %d", e.port)
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	log.Printf("[engram-serve] warning: serve on port %d did not become healthy within 5s", e.port)
	return nil
}

// Supervise watches the serve process and restarts it if it exits. Should be
// run as a goroutine. Stops when ctx is done.
func (e *EngramServer) Supervise(ctx context.Context) {
	for {
		if e.proc == nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
				if err := e.Start(); err != nil {
					log.Printf("[engram-serve] restart failed: %v", err)
				}
				continue
			}
		}

		// Wait for the process to exit.
		done := make(chan error, 1)
		proc := e.proc
		go func() { _, err := proc.Wait(); done <- err }()

		select {
		case <-ctx.Done():
			return
		case err := <-done:
			if ctx.Err() != nil {
				return
			}
			log.Printf("[engram-serve] serve exited (%v) — restarting", err)
			e.proc = nil
			// Brief pause before restart to avoid tight loops on repeated failures.
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
			}
			if err := e.Start(); err != nil {
				log.Printf("[engram-serve] restart failed: %v", err)
			}
		}
	}
}

// Stop kills the supervised serve process. Called on daemon shutdown.
func (e *EngramServer) Stop() {
	if e.proc != nil {
		log.Printf("[engram-serve] stopping pid %d", e.proc.Pid)
		_ = e.proc.Kill()
		e.proc = nil
	}
}

// Port returns the port this server is bound to.
func (e *EngramServer) Port() int {
	return e.port
}

// Bin returns the resolved path to the engram binary, or empty string if not resolved.
func (e *EngramServer) Bin() string {
	return e.bin
}

// pidOnPort returns the PID of the process listening on the given TCP port
// (127.0.0.1 only), or 0 if none. Uses ss(1) (preferred) or lsof(1) as fallback.
func pidOnPort(port int) int {
	// Try ss first (iproute2, available on all modern Linux).
	pid := pidOnPortSS(port)
	if pid > 0 {
		return pid
	}
	return pidOnPortLSOF(port)
}

func pidOnPortSS(port int) int {
	out, err := exec.Command("ss", "-ltnp", fmt.Sprintf("sport = :%d", port)).Output()
	if err != nil {
		return 0
	}
	// Look for "pid=<N>" in the output.
	for _, line := range strings.Split(string(out), "\n") {
		if !strings.Contains(line, fmt.Sprintf(":%d", port)) {
			continue
		}
		if idx := strings.Index(line, "pid="); idx >= 0 {
			rest := line[idx+4:]
			end := strings.IndexAny(rest, ",)")
			if end < 0 {
				end = len(rest)
			}
			var pid int
			fmt.Sscanf(rest[:end], "%d", &pid)
			if pid > 0 {
				return pid
			}
		}
	}
	return 0
}

func pidOnPortLSOF(port int) int {
	out, err := exec.Command("lsof", "-iTCP", fmt.Sprintf(":%d", port), "-sTCP:LISTEN", "-t").Output()
	if err != nil {
		return 0
	}
	var pid int
	fmt.Sscanf(strings.TrimSpace(string(out)), "%d", &pid)
	return pid
}

// engramDataDirFromPID reads the ENGRAM_DATA_DIR value from /proc/<pid>/environ.
// Returns an empty string (not an error) when the variable is absent.
func engramDataDirFromPID(pid int) (string, error) {
	path := fmt.Sprintf("/proc/%d/environ", pid)
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	scanner.Split(splitNull)
	for scanner.Scan() {
		kv := scanner.Text()
		if strings.HasPrefix(kv, "ENGRAM_DATA_DIR=") {
			return strings.TrimPrefix(kv, "ENGRAM_DATA_DIR="), nil
		}
	}
	return "", nil
}

// splitNull is a bufio.SplitFunc that splits on null bytes (used to parse
// /proc/<pid>/environ, which is a null-separated list of KEY=VALUE pairs).
func splitNull(data []byte, atEOF bool) (advance int, token []byte, err error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.IndexByte(data, 0); i >= 0 {
		return i + 1, data[:i], nil
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}
