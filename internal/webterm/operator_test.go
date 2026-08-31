package webterm

import (
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"

	"github.com/nats-io/nats-server/v2/server"
)

// startEmbeddedNATSJS starts an embedded NATS server with JetStream (needed for KV).
func startEmbeddedNATSJS(t *testing.T) (*server.Server, string) {
	t.Helper()
	opts := &server.Options{Port: -1, JetStream: true, StoreDir: t.TempDir()}
	ns, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("start NATS: %v", err)
	}
	ns.Start()
	if !ns.ReadyForConnections(5 * time.Second) {
		t.Fatal("NATS not ready")
	}
	return ns, fmt.Sprintf("nats://127.0.0.1:%d", ns.Addr().(*net.TCPAddr).Port)
}

func newJSClient(t *testing.T, url string) *pnats.Client {
	t.Helper()
	c := pnats.NewClient(&config.Credentials{NATS: config.NATSConfig{URL: url}})
	if err := c.Connect(); err != nil {
		t.Fatalf("connect: %v", err)
	}
	return c
}

func newKVClient(c *pnats.Client) *pnats.KV { return pnats.NewKV(c) }

func TestOperatorDiscovery(t *testing.T) {
	ns, url := startEmbeddedNATSJS(t)
	defer ns.Shutdown()

	client := newJSClient(t, url)
	defer client.Close()
	kv := newKVClient(client)

	if err := PublishOwner(kv, "huge", "lelongs"); err != nil {
		t.Fatalf("publish owner: %v", err)
	}

	store := &OwnerStore{KV: kv}
	if got := store.Owner("huge"); got != "lelongs" {
		t.Fatalf("owner = %q, want lelongs", got)
	}
	if !store.IsOperator("huge", "LELONGS") { // case-insensitive
		t.Fatal("expected LELONGS to match owner lelongs")
	}
	if store.IsOperator("huge", "someone") {
		t.Fatal("non-owner must not be operator")
	}
	if store.IsOperator("unknown-vignoble", "lelongs") {
		t.Fatal("unknown vignoble must not grant operator")
	}
	if store.IsOperator("huge", "") {
		t.Fatal("empty username must not be operator")
	}
}

func TestFunderStore(t *testing.T) {
	ns, url := startEmbeddedNATSJS(t)
	defer ns.Shutdown()
	client := newJSClient(t, url)
	defer client.Close()
	kv := newKVClient(client)

	if err := PublishFunder(kv, "exohub", "myworker--proj-abc", "funder1"); err != nil {
		t.Fatalf("publish funder: %v", err)
	}

	store := &FunderStore{KV: kv}

	if got := store.Funder("exohub", "myworker--proj-abc"); got != "funder1" {
		t.Fatalf("Funder = %q, want funder1", got)
	}
	if !store.IsFunder("exohub", "myworker--proj-abc", "funder1") {
		t.Fatal("exact match must be funder")
	}
	if !store.IsFunder("exohub", "myworker--proj-abc", "FUNDER1") {
		t.Fatal("case-insensitive match must be funder")
	}
	if store.IsFunder("exohub", "myworker--proj-abc", "other") {
		t.Fatal("non-matching user must not be funder")
	}
	if store.IsFunder("exohub", "other-session", "funder1") {
		t.Fatal("different target must not be funder")
	}
	if store.IsFunder("other-vignoble", "myworker--proj-abc", "funder1") {
		t.Fatal("different vignoble must not be funder")
	}
	if store.IsFunder("exohub", "myworker--proj-abc", "") {
		t.Fatal("empty username must not be funder")
	}
}

func TestPublishFunderNoOp(t *testing.T) {
	// PublishFunder must be a no-op (no panic, no error) on empty fields.
	if err := PublishFunder(nil, "v", "t", "u"); err != nil {
		t.Fatal(err)
	}
	if err := PublishFunder(nil, "", "t", "u"); err != nil {
		t.Fatal(err)
	}
}

func TestOwnedBy(t *testing.T) {
	ns, url := startEmbeddedNATSJS(t)
	defer ns.Shutdown()
	client := newJSClient(t, url)
	defer client.Close()
	kv := newKVClient(client)

	for v, owner := range map[string]string{"exohub": "lelongs", "genomics": "LELONGS", "misc": "someone"} {
		if err := PublishOwner(kv, v, owner); err != nil {
			t.Fatal(err)
		}
	}
	store := &OwnerStore{KV: kv}
	owned := store.OwnedBy("lelongs")
	if len(owned) != 2 || owned[0] != "exohub" || owned[1] != "genomics" {
		t.Fatalf("OwnedBy(lelongs) = %v, want [exohub genomics] (sorted, case-insensitive)", owned)
	}
	if len(store.OwnedBy("someone")) != 1 {
		t.Fatalf("OwnedBy(someone) should be [misc]")
	}
	if store.OwnedBy("") != nil {
		t.Fatal("OwnedBy(\"\") must be nil")
	}
}
