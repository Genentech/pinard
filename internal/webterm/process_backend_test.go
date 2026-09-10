package webterm

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"sync"
	"testing"
	"time"

	"fmt"
	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"net"
)

// fakeReadWriter is a synchronised in-memory pipe used as a stand-in for a PTY.
type fakeReadWriter struct {
	mu     sync.Mutex
	buf    bytes.Buffer
	writes [][]byte // captures what was written (steer keystrokes)
	readCh chan []byte
	closed bool
}

func newFakeRW(initial []byte) *fakeReadWriter {
	f := &fakeReadWriter{readCh: make(chan []byte, 16)}
	if len(initial) > 0 {
		f.readCh <- initial
	}
	return f
}

func (f *fakeReadWriter) Read(p []byte) (int, error) {
	data, ok := <-f.readCh
	if !ok {
		return 0, io.EOF
	}
	n := copy(p, data)
	return n, nil
}

func (f *fakeReadWriter) Write(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp := make([]byte, len(p))
	copy(cp, p)
	f.writes = append(f.writes, cp)
	return len(p), nil
}

func (f *fakeReadWriter) close() { close(f.readCh) }

func (f *fakeReadWriter) writtenCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.writes)
}

// startEmbeddedNATSLocal starts an embedded NATS server and returns the URL.
// (We define it locally here to avoid a naming conflict with responder_test.go;
// the two are in the same package so we use a different name.)
func startEmbeddedNATSLocal(t *testing.T) (*server.Server, string) {
	t.Helper()
	opts := &server.Options{Port: -1, StoreDir: t.TempDir()}
	ns, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("start embedded NATS: %v", err)
	}
	ns.Start()
	if !ns.ReadyForConnections(5 * time.Second) {
		t.Fatal("NATS not ready")
	}
	return ns, fmt.Sprintf("nats://127.0.0.1:%d", ns.Addr().(*net.TCPAddr).Port)
}

// runProcessResponder starts a Responder with a ProcessBackend using fakeRW.
func runProcessResponder(t *testing.T, nc *nats.Conn, vignoble, sessionName string, fakeRW *fakeReadWriter) {
	t.Helper()
	backend := &ProcessBackend{Target: sessionName, PTY: fakeRW}
	resp := &Responder{
		NC:          nc,
		Vignoble:    vignoble,
		GrantSecret: grantSecret,
		MaxViewers:  4,
		Backend:     backend,
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = resp.Run(ctx) }()
	time.Sleep(100 * time.Millisecond)
}

// TestProcessBackendHasSession verifies HasSession matches the configured target.
func TestProcessBackendHasSession(t *testing.T) {
	b := &ProcessBackend{Target: "myworker"}
	if !b.HasSession("myworker") {
		t.Error("HasSession should return true for the configured target")
	}
	if b.HasSession("other") {
		t.Error("HasSession should return false for a different target")
	}
}

// TestProcessBackendGrantFailClosedMissingGrant verifies that a responder with a
// ProcessBackend stays silent (fail-closed) for a missing grant.
func TestProcessBackendGrantFailClosedMissingGrant(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	fakeRW := newFakeRW(nil)
	runProcessResponder(t, nc, "test", "myworker", fakeRW)

	// No grant → responder must stay silent.
	req, _ := json.Marshal(ReqMsg{Grant: "", ViewerID: "v1", Cols: 80, Rows: 24})
	_, err := nc.Request(ReqSubject("test"), req, 500*time.Millisecond)
	if err == nil {
		t.Fatal("expected silence (no reply) for missing grant, got a reply")
	}
}

// TestProcessBackendGrantFailClosedBadGrant verifies that a bad (wrong-secret)
// grant is silently ignored.
func TestProcessBackendGrantFailClosedBadGrant(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	fakeRW := newFakeRW(nil)
	runProcessResponder(t, nc, "test", "myworker", fakeRW)

	bad, _ := SignGrant(Grant{Vignoble: "test", Target: "myworker", Mode: ModeRO, Exp: time.Now().Add(time.Minute).Unix()}, []byte("wrong-secret"))
	req, _ := json.Marshal(ReqMsg{Grant: bad, ViewerID: "v1", Cols: 80, Rows: 24})
	_, err := nc.Request(ReqSubject("test"), req, 500*time.Millisecond)
	if err == nil {
		t.Fatal("expected silence for bad grant, got a reply")
	}
}

// TestProcessBackendGrantFailClosedExpired verifies that an expired grant is
// silently ignored.
func TestProcessBackendGrantFailClosedExpired(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	fakeRW := newFakeRW(nil)
	runProcessResponder(t, nc, "test", "myworker", fakeRW)

	expired, _ := SignGrant(Grant{Vignoble: "test", Target: "myworker", Mode: ModeRO, Exp: time.Now().Add(-time.Minute).Unix()}, grantSecret)
	req, _ := json.Marshal(ReqMsg{Grant: expired, ViewerID: "v1", Cols: 80, Rows: 24})
	_, err := nc.Request(ReqSubject("test"), req, 500*time.Millisecond)
	if err == nil {
		t.Fatal("expected silence for expired grant, got a reply")
	}
}

// TestProcessBackendGrantFailClosedWrongVignoble verifies that a grant for a
// different vignoble is silently ignored.
func TestProcessBackendGrantFailClosedWrongVignoble(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	fakeRW := newFakeRW(nil)
	runProcessResponder(t, nc, "myvigne", "myworker", fakeRW)

	foreign, _ := SignGrant(Grant{Vignoble: "other-vigne", Target: "myworker", Mode: ModeRO, Exp: time.Now().Add(time.Minute).Unix()}, grantSecret)
	req, _ := json.Marshal(ReqMsg{Grant: foreign, ViewerID: "v1", Cols: 80, Rows: 24})
	_, err := nc.Request(ReqSubject("myvigne"), req, 500*time.Millisecond)
	if err == nil {
		t.Fatal("expected silence for wrong-vignoble grant, got a reply")
	}
}

// TestProcessBackendROGrantNoWrite verifies that a RO grant does NOT subscribe
// to InSubject, so no keystrokes reach the PTY.
func TestProcessBackendROGrantNoWrite(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	sessionName := "myworker"
	vignoble := "testvigne"
	// The fake PTY returns one byte then blocks.
	fakeRW := newFakeRW([]byte("hello"))
	runProcessResponder(t, nc, vignoble, sessionName, fakeRW)

	// Mint a RO grant.
	roGrant, _ := SignGrant(Grant{
		Vignoble: vignoble,
		Target:   sessionName,
		Mode:     ModeRO,
		Exp:      time.Now().Add(time.Minute).Unix(),
	}, grantSecret)

	viewerID := "viewer-ro"
	req, _ := json.Marshal(ReqMsg{Grant: roGrant, ViewerID: viewerID, Cols: 80, Rows: 24})
	msg, err := nc.Request(ReqSubject(vignoble), req, 2*time.Second)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	var reply ReqReply
	json.Unmarshal(msg.Data, &reply)
	if !reply.OK {
		t.Fatalf("expected OK reply for valid RO grant, got: %s", reply.Reason)
	}

	// Publish keystrokes on InSubject — should be ignored by the responder.
	inSubject := InSubject(vignoble, viewerID)
	for i := 0; i < 5; i++ {
		_ = nc.Publish(inSubject, []byte("keystroke"))
	}
	// Give the responder time to (not) process them.
	time.Sleep(100 * time.Millisecond)

	if n := fakeRW.writtenCount(); n != 0 {
		t.Errorf("RO grant: expected 0 writes to PTY, got %d", n)
	}
}

// TestProcessBackendRWGrantAllowsWrite verifies that a RW grant subscribes to
// InSubject and forwards keystrokes to the PTY.
func TestProcessBackendRWGrantAllowsWrite(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	sessionName := "myworker"
	vignoble := "testvigne"
	fakeRW := newFakeRW([]byte("hello"))
	runProcessResponder(t, nc, vignoble, sessionName, fakeRW)

	// Mint a RW grant.
	rwGrant, _ := SignGrant(Grant{
		Vignoble: vignoble,
		Target:   sessionName,
		Mode:     ModeRW,
		Exp:      time.Now().Add(time.Minute).Unix(),
	}, grantSecret)

	viewerID := "viewer-rw"
	req, _ := json.Marshal(ReqMsg{Grant: rwGrant, ViewerID: viewerID, Cols: 80, Rows: 24})
	msg, err := nc.Request(ReqSubject(vignoble), req, 2*time.Second)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	var reply ReqReply
	json.Unmarshal(msg.Data, &reply)
	if !reply.OK {
		t.Fatalf("expected OK reply for valid RW grant, got: %s", reply.Reason)
	}

	// Give the responder a moment to set up the InSubject subscription.
	time.Sleep(80 * time.Millisecond)

	// Publish keystrokes on InSubject — should be forwarded to the PTY.
	inSubject := InSubject(vignoble, viewerID)
	keystroke := []byte("x")
	_ = nc.Publish(inSubject, keystroke)
	_ = nc.Flush()

	// Wait for the write to propagate.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if fakeRW.writtenCount() >= 1 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}

	if n := fakeRW.writtenCount(); n < 1 {
		t.Errorf("RW grant: expected at least 1 write to PTY, got %d", n)
	}
}

// TestProcessBackendOutputStreamed verifies that output from the PTY is published
// on the viewer's OutSubject.
func TestProcessBackendOutputStreamed(t *testing.T) {
	ns, url := startEmbeddedNATSLocal(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	sessionName := "myworker"
	vignoble := "testvigne"
	fakeRW := newFakeRW([]byte("PTY output"))
	runProcessResponder(t, nc, vignoble, sessionName, fakeRW)

	grant, _ := SignGrant(Grant{
		Vignoble: vignoble,
		Target:   sessionName,
		Mode:     ModeRO,
		Exp:      time.Now().Add(time.Minute).Unix(),
	}, grantSecret)

	viewerID := "viewer-out"
	received := make(chan []byte, 4)
	outSub, err := nc.Subscribe(OutSubject(vignoble, viewerID), func(m *nats.Msg) {
		cp := make([]byte, len(m.Data))
		copy(cp, m.Data)
		select {
		case received <- cp:
		default:
		}
	})
	if err != nil {
		t.Fatalf("subscribe out: %v", err)
	}
	defer outSub.Unsubscribe()

	req, _ := json.Marshal(ReqMsg{Grant: grant, ViewerID: viewerID, Cols: 80, Rows: 24})
	msg, err := nc.Request(ReqSubject(vignoble), req, 2*time.Second)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	var reply ReqReply
	json.Unmarshal(msg.Data, &reply)
	if !reply.OK {
		t.Fatalf("unexpected rejection: %s", reply.Reason)
	}

	select {
	case data := <-received:
		if !bytes.Contains(data, []byte("PTY output")) {
			t.Errorf("output: expected 'PTY output', got %q", data)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for PTY output on OutSubject")
	}
}

// TestProcessBackendROWriteBlocked verifies that the read-only wrapper returned
// by ProcessBackend.Attach when writable=false physically rejects writes, giving
// the same belt-and-suspenders protection as `tmux attach -r` on TmuxBackend.
func TestProcessBackendROWriteBlocked(t *testing.T) {
	fakeRW := newFakeRW(nil)
	b := &ProcessBackend{Target: "myworker", PTY: fakeRW}

	rw, cleanup, _, err := b.Attach("myworker", 80, 24, false /* read-only */)
	if err != nil {
		t.Fatalf("Attach: %v", err)
	}
	defer cleanup()

	n, werr := rw.Write([]byte("keystroke"))
	if werr == nil {
		t.Errorf("expected write error from read-only wrapper, got nil (n=%d)", n)
	}
	if n != 0 {
		t.Errorf("expected 0 bytes written, got %d", n)
	}
	// Reads should still work.
	fakeRW.readCh <- []byte("output")
	buf := make([]byte, 16)
	nr, _ := rw.Read(buf)
	if nr == 0 {
		t.Error("expected to read output bytes from read-only wrapper")
	}
}

// TestProcessBackendRWWriteAllowed verifies that the writable=true path returns
// a fully read-write interface (no write-blocking wrapper).
func TestProcessBackendRWWriteAllowed(t *testing.T) {
	fakeRW := newFakeRW(nil)
	b := &ProcessBackend{Target: "myworker", PTY: fakeRW}

	rw, cleanup, _, err := b.Attach("myworker", 80, 24, true /* read-write */)
	if err != nil {
		t.Fatalf("Attach: %v", err)
	}
	defer cleanup()

	n, werr := rw.Write([]byte("keystroke"))
	if werr != nil {
		t.Errorf("expected write to succeed for RW attach, got: %v", werr)
	}
	if n != 9 {
		t.Errorf("expected 9 bytes written, got %d", n)
	}
}
