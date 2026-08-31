package watcher

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/state"
	"net"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

func startEmbeddedNATS(t *testing.T) (*server.Server, string) {
	t.Helper()
	opts := &server.Options{
		Port:      -1, // random port
		JetStream: true,
		StoreDir:  t.TempDir(),
	}
	ns, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("start embedded NATS: %v", err)
	}
	ns.Start()
	if !ns.ReadyForConnections(5 * time.Second) {
		t.Fatal("NATS not ready")
	}
	url := fmt.Sprintf("nats://127.0.0.1:%d", ns.Addr().(*net.TCPAddr).Port)
	return ns, url
}

func testClient(t *testing.T, url string) *pnats.Client {
	t.Helper()
	creds := &config.Credentials{
		NATS: config.NATSConfig{URL: url},
	}
	c := pnats.NewClient(creds)
	if err := c.Connect(); err != nil {
		t.Fatalf("connect: %v", err)
	}
	return c
}

func TestIntegration_NotifyPublishesAndReceives(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	// Use raw nats connection for reliability
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()

	sub, err := nc.SubscribeSync("pinard.test.notifications")
	if err != nil {
		t.Fatal(err)
	}
	nc.Flush()

	data, _ := json.Marshal(map[string]any{
		"message":   "hello from test",
		"timestamp": time.Now().Format(time.RFC3339),
	})
	nc.Publish("pinard.test.notifications", data)
	nc.Flush()

	msg, err := sub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("no message received: %v", err)
	}

	var payload map[string]any
	json.Unmarshal(msg.Data, &payload)
	if payload["message"] != "hello from test" {
		t.Errorf("expected 'hello from test', got %v", payload["message"])
	}
}

func TestIntegration_KVOperations(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	client := testClient(t, url)
	defer client.Close()

	// Create KV bucket
	js, _ := client.Conn().JetStream()
	js.CreateKeyValue(&nats.KeyValueConfig{Bucket: "test-agents"})

	kv := pnats.NewKV(client)

	// Put
	err := kv.Put("test-agents", "worker-1", map[string]any{"state": "running", "tempo": "active"})
	if err != nil {
		t.Fatalf("put: %v", err)
	}

	// Get
	val, err := kv.Get("test-agents", "worker-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if val["state"] != "running" {
		t.Errorf("expected state=running, got %v", val["state"])
	}

	// Delete
	kv.Del("test-agents", "worker-1")
	val2, _ := kv.Get("test-agents", "worker-1")
	if val2 != nil {
		t.Errorf("expected nil after delete, got %v", val2)
	}
}

func TestIntegration_IssueWatcherPublishesEvent(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	nc, _ := nats.Connect(url)
	defer nc.Close()

	sub, _ := nc.SubscribeSync("pinard.test.issues.new")
	nc.Flush()

	data, _ := json.Marshal(map[string]any{
		"project":    "exo-cli",
		"iid":       42,
		"title":     "Fix auth",
		"auto_spawn": true,
	})
	nc.Publish("pinard.test.issues.new", data)
	nc.Flush()

	msg, err := sub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("no event received: %v", err)
	}

	var payload map[string]any
	json.Unmarshal(msg.Data, &payload)
	if payload["project"] != "exo-cli" {
		t.Errorf("expected project=exo-cli, got %v", payload["project"])
	}
	if payload["auto_spawn"] != true {
		t.Errorf("expected auto_spawn=true, got %v", payload["auto_spawn"])
	}
}

func TestIntegration_MRWatcherStateWithNATS(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	client := testClient(t, url)
	defer client.Close()

	// Create KV
	js, _ := client.Conn().JetStream()
	js.CreateKeyValue(&nats.KeyValueConfig{Bucket: "pinard-agents"})

	kv := pnats.NewKV(client)

	// Set up state
	dir := t.TempDir()
	mrState, _ := state.Load[state.MRWatcherState](filepath.Join(dir, "mr-watcher.yaml"))
	mrState.Update(func(s *state.MRWatcherState) {
		s.Watched = map[string]*state.WatchedMR{
			"worker-1": {Name: "worker-1", Project: "exo-cli", Repo: "group/exo-cli", MR: 42},
		}
	})

	// Simulate session alive check via KV
	kv.Put("pinard-agents", "worker-1", map[string]any{"state": "running", "tempo": "active"})

	val, _ := kv.Get("pinard-agents", "worker-1")
	if val == nil || val["state"] != "running" {
		t.Fatal("worker-1 should be alive in KV")
	}
}

func TestIntegration_ReviewCommentDispatched(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	nc, _ := nats.Connect(url)
	defer nc.Close()

	sub, _ := nc.SubscribeSync("pinard.test.agents.worker-1.inbox")
	nc.Flush()

	data, _ := json.Marshal(map[string]any{
		"message": "Review feedback on MR !42:\n- @reviewer: Fix the auth check",
		"from":    "conductor",
	})
	nc.Publish("pinard.test.agents.worker-1.inbox", data)
	nc.Flush()

	msg, err := sub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("no inbox message: %v", err)
	}

	var payload map[string]any
	json.Unmarshal(msg.Data, &payload)
	if payload["from"] != "conductor" {
		t.Errorf("expected from=conductor, got %v", payload["from"])
	}
}

func TestIntegration_EventNotDispatchedToWrongWorker(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()

	nc, _ := nats.Connect(url)
	defer nc.Close()

	sub, _ := nc.SubscribeSync("pinard.test.agents.worker-2.inbox")
	nc.Flush()

	data, _ := json.Marshal(map[string]any{"mr": 42})
	nc.Publish("pinard.test.agents.worker-1.events.pipeline_failed", data)
	nc.Flush()

	// Worker-2 should not get anything
	msg, err := sub.NextMsg(500 * time.Millisecond)
	if err == nil {
		t.Errorf("worker-2 should not receive worker-1's event, got: %s", msg.Data)
	}
}
