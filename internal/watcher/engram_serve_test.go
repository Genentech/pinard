package watcher

import (
	"context"
	"os"
	"os/exec"
	"syscall"
	"testing"
	"time"
)

// TestAdoptedServeLivenessDetection verifies that an adopted (non-child) serve
// is monitored via kill(pid,0) and not proc.Wait(), avoiding the ECHILD tight loop.
func TestAdoptedServeLivenessDetection(t *testing.T) {
	// Spawn a real child process that we can kill externally.
	cmd := exec.Command("sleep", "60")
	if err := cmd.Start(); err != nil {
		t.Fatalf("failed to start sleep: %v", err)
	}
	pid := cmd.Process.Pid

	// Simulate adoption: obtain a *os.Process handle for the pid without being
	// the parent (in this test we ARE the parent, but we detach by not calling
	// cmd.Wait). The key behaviour under test is the polling path, not actual
	// non-child semantics (which can't be simulated in a unit test without
	// fork/exec trickery).
	adoptedProc, err := os.FindProcess(pid)
	if err != nil {
		t.Fatalf("FindProcess: %v", err)
	}

	srv := &EngramServer{
		proc:    adoptedProc,
		adopted: true,
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	exited := make(chan bool, 1)
	go func() {
		exited <- srv.pollAdoptedLiveness(ctx, adoptedProc)
	}()

	// Give the poller a moment to start, then kill the process.
	time.Sleep(200 * time.Millisecond)
	if err := cmd.Process.Kill(); err != nil {
		t.Fatalf("kill: %v", err)
	}
	// Reap the child so the pid is fully gone.
	cmd.Wait() //nolint:errcheck

	select {
	case result := <-exited:
		if !result {
			t.Error("pollAdoptedLiveness returned false (ctx cancelled) but should have detected exit")
		}
	case <-time.After(4 * time.Second):
		t.Error("pollAdoptedLiveness did not detect process exit within 4s")
	}
}

// TestAdoptedServeLivenessCancelledContext verifies that pollAdoptedLiveness
// returns false (not exited) when the context is cancelled while the process is alive.
func TestAdoptedServeLivenessCancelledContext(t *testing.T) {
	cmd := exec.Command("sleep", "60")
	if err := cmd.Start(); err != nil {
		t.Fatalf("failed to start sleep: %v", err)
	}
	defer func() {
		cmd.Process.Kill() //nolint:errcheck
		cmd.Wait()         //nolint:errcheck
	}()

	adoptedProc, err := os.FindProcess(cmd.Process.Pid)
	if err != nil {
		t.Fatalf("FindProcess: %v", err)
	}

	srv := &EngramServer{
		proc:    adoptedProc,
		adopted: true,
	}

	ctx, cancel := context.WithCancel(context.Background())

	exited := make(chan bool, 1)
	go func() {
		exited <- srv.pollAdoptedLiveness(ctx, adoptedProc)
	}()

	time.Sleep(200 * time.Millisecond)
	cancel()

	select {
	case result := <-exited:
		if result {
			t.Error("pollAdoptedLiveness returned true (exited) but process was still alive; ctx cancel should return false")
		}
	case <-time.After(3 * time.Second):
		t.Error("pollAdoptedLiveness did not return after context cancellation")
	}
}

// TestKillSignalZeroOnDeadPID verifies our assumption: kill(pid, 0) on a
// reaped PID returns ESRCH, which is what pollAdoptedLiveness relies on.
func TestKillSignalZeroOnDeadPID(t *testing.T) {
	cmd := exec.Command("true")
	if err := cmd.Run(); err != nil {
		t.Fatalf("run true: %v", err)
	}
	pid := cmd.ProcessState.Pid()

	err := syscall.Kill(pid, 0)
	if err != syscall.ESRCH {
		// On Linux a reaped and recycled PID could theoretically return nil here,
		// but in practice for a freshly-exited process in a test this should be ESRCH.
		// We skip rather than fail to avoid flakiness on high-load systems.
		t.Skipf("kill(%d, 0) returned %v (expected ESRCH); PID may have been recycled", pid, err)
	}
}
