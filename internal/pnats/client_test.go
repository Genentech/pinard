package pnats

import (
	"os"
	"testing"
	"time"

	"github.com/Genentech/pinard/internal/config"
)

func hasLiveCreds() bool {
	return os.Getenv("PINARD_NATS_PASSWORD") != "" && os.Getenv("PINARD_NATS_USER") != ""
}

func liveCreds() *config.Credentials {
	return &config.Credentials{
		NATS: config.NATSConfig{
			URL:         os.Getenv("PINARD_NATS_URL"),
			User:        os.Getenv("PINARD_NATS_USER"),
			PasswordEnv: "PINARD_NATS_PASSWORD",
		},
	}
}

func TestConnect_LiveNATS(t *testing.T) {
	if !hasLiveCreds() {
		t.Skip("PINARD_NATS_USER and PINARD_NATS_PASSWORD required")
	}

	c := NewClient(liveCreds())
	defer c.Close()

	if err := c.Connect(); err != nil {
		t.Fatalf("connect failed: %v", err)
	}
	if c.nc == nil || !c.nc.IsConnected() {
		t.Fatal("expected connected")
	}
}

func TestPublish_LiveNATS(t *testing.T) {
	if !hasLiveCreds() {
		t.Skip("PINARD_NATS_USER and PINARD_NATS_PASSWORD required")
	}

	c := NewClient(liveCreds())
	defer c.Close()

	err := c.Publish("pinard.test.go-smoke", map[string]any{
		"test": true,
		"ts":   time.Now().Unix(),
	})
	if err != nil {
		t.Fatalf("publish failed: %v", err)
	}
}

func TestKV_LiveNATS(t *testing.T) {
	if !hasLiveCreds() {
		t.Skip("PINARD_NATS_USER and PINARD_NATS_PASSWORD required")
	}

	c := NewClient(liveCreds())
	defer c.Close()
	if err := c.Connect(); err != nil {
		t.Fatalf("connect: %v", err)
	}

	kv := NewKV(c)
	key := "go-smoke-test"

	// Put
	if err := kv.Put("pinard-agents", key, map[string]any{"status": "testing"}); err != nil {
		t.Fatalf("kv put: %v", err)
	}

	// Get
	val, err := kv.Get("pinard-agents", key)
	if err != nil {
		t.Fatalf("kv get: %v", err)
	}
	if val["status"] != "testing" {
		t.Errorf("expected status=testing, got %v", val["status"])
	}

	// Delete
	if err := kv.Del("pinard-agents", key); err != nil {
		t.Fatalf("kv del: %v", err)
	}

	// Verify deleted
	val2, _ := kv.Get("pinard-agents", key)
	if val2 != nil {
		t.Errorf("expected nil after delete, got %v", val2)
	}
}

func TestRejectsNoAuth_LiveNATS(t *testing.T) {
	if !hasLiveCreds() {
		t.Skip("PINARD_NATS_USER and PINARD_NATS_PASSWORD required")
	}

	// Connect without credentials — should fail
	c := NewClient(&config.Credentials{
		NATS: config.NATSConfig{
			URL: os.Getenv("PINARD_NATS_URL"),
		},
	})
	defer c.Close()

	err := c.Connect()
	if err == nil {
		t.Fatal("expected auth error, got nil")
	}
}
