package pnats

import (
	"crypto/tls"
	"crypto/x509"
	"embed"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/nats-io/nats.go"
)

//go:embed certs/*.crt
var embeddedCerts embed.FS

type Client struct {
	nc   *nats.Conn
	js   nats.JetStreamContext
	mu   sync.Mutex
	cfg  config.NATSConfig
	creds *config.Credentials
}

func NewClient(creds *config.Credentials) *Client {
	return &Client{
		cfg:   creds.NATS,
		creds: creds,
	}
}

func (c *Client) Connect() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.nc != nil && c.nc.IsConnected() {
		return nil
	}

	url := c.creds.NATSUrl()
	opts := []nats.Option{
		nats.Timeout(5 * time.Second),
		nats.MaxReconnects(5),
		nats.ReconnectWait(2 * time.Second),
		nats.DontRandomize(),
		nats.NoEcho(),
	}
	// Don't follow server-advertised URLs (pod IPs not reachable from outside)
	opts = append(opts, nats.CustomReconnectDelay(func(attempts int) time.Duration {
		return 2 * time.Second
	}))

	if c.cfg.User != "" {
		pass := c.creds.NATSPassword()
		opts = append(opts, nats.UserInfo(c.cfg.User, pass))
	}

	if c.cfg.Credentials != "" {
		opts = append(opts, nats.UserCredentials(c.cfg.Credentials))
	}

	// TLS config for wss:// connections
	if strings.HasPrefix(url, "wss://") {
		tlsCfg := &tls.Config{InsecureSkipVerify: true}
		home, _ := os.UserHomeDir()
		certsDir := filepath.Join(home, ".config", "pinard", "certs")
		if certs, err := loadCACerts(certsDir); err == nil && certs != nil {
			tlsCfg.RootCAs = certs
			tlsCfg.InsecureSkipVerify = false
		}
		opts = append(opts, nats.Secure(tlsCfg))
	}

	nc, err := nats.Connect(url, opts...)
	if err != nil {
		return fmt.Errorf("NATS connect failed (%s): %w", url, err)
	}

	js, err := nc.JetStream()
	if err != nil {
		nc.Close()
		return fmt.Errorf("JetStream init failed: %w", err)
	}

	c.nc = nc
	c.js = js
	c.ensureStreams()
	return nil
}

func (c *Client) ensureStreams() {
	streams := []struct {
		name     string
		subjects []string
		maxAge   time.Duration // 0 = no age limit
	}{
		{"pinard-agent-events", []string{StreamSubjectAgentEvents}, 0},
		{"pinard-inboxes", []string{StreamSubjectInboxes}, 0},
		{"pinard-issues", []string{"pinard.*.issues.>"}, 0},
		{"pinard-notifications", []string{"pinard.*.notifications", "pinard.*.parcelles.*.notifications"}, 0},
		{"pinard-scheduler-events", []string{"pinard.*.schedules.>"}, 0},
		{"pinard-processes", []string{StreamSubjectProcesses}, 0},
		{"pinard-memory", []string{StreamSubjectMemory}, 7 * 24 * time.Hour},
	}
	for _, s := range streams {
		info, err := c.js.StreamInfo(s.name)
		if err != nil {
			log.Printf("[nats] Stream %s not found, creating...", s.name)
			_, err = c.js.AddStream(&nats.StreamConfig{
				Name:      s.name,
				Subjects:  s.subjects,
				Retention: nats.LimitsPolicy,
				Storage:   nats.FileStorage,
				Replicas:  1,
				MaxAge:    s.maxAge,
			})
			if err != nil {
				log.Printf("[nats] Failed to create stream %s: %v", s.name, err)
			} else {
				log.Printf("[nats] Created stream %s", s.name)
			}
		} else if fmt.Sprintf("%v", info.Config.Subjects) != fmt.Sprintf("%v", s.subjects) {
			log.Printf("[nats] Stream %s subjects mismatch: have %v, want %v — updating", s.name, info.Config.Subjects, s.subjects)
			cfg := info.Config
			cfg.Subjects = s.subjects
			_, err = c.js.UpdateStream(&cfg)
			if err != nil {
				log.Printf("[nats] Failed to update stream %s: %v", s.name, err)
			} else {
				log.Printf("[nats] Updated stream %s", s.name)
			}
		}
	}
}

// Request sends a NATS request and waits up to timeout for a reply.
// Uses core NATS (not JetStream) — appropriate for request-reply subjects
// that live outside the JetStream stream hierarchy.
func (c *Client) Request(subject string, payload any, timeout time.Duration) (*nats.Msg, error) {
	if err := c.Connect(); err != nil {
		return nil, err
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return c.nc.Request(subject, data, timeout)
}

func (c *Client) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.nc != nil {
		c.nc.Close()
		c.nc = nil
		c.js = nil
	}
}

func (c *Client) Publish(subject string, payload any) error {
	if err := c.Connect(); err != nil {
		return err
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	// Use JetStream publish for guaranteed delivery to streams.
	// Falls back to core NATS for subjects not captured by any stream.
	if c.js != nil {
		ack, err := c.js.Publish(subject, data)
		if err == nil {
			log.Printf("[nats] JS publish OK: %s (stream=%s seq=%d)", subject, ack.Stream, ack.Sequence)
			return nil
		}
		// If JetStream rejects (no matching stream), fall back to core NATS
		log.Printf("[nats] JS publish failed for %s: %v (falling back to core)", subject, err)
		if err == nats.ErrNoStreamResponse {
			if err := c.nc.Publish(subject, data); err != nil {
				return err
			}
			return c.nc.Flush()
		}
		return err
	}
	if err := c.nc.Publish(subject, data); err != nil {
		return err
	}
	return c.nc.Flush()
}

func loadCACerts(dir string) (*x509.CertPool, error) {
	pool := x509.NewCertPool()
	loaded := 0

	// Try embedded certs first (compiled into the binary)
	entries, err := embeddedCerts.ReadDir("certs")
	if err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				continue
			}
			data, err := embeddedCerts.ReadFile("certs/" + entry.Name())
			if err != nil {
				continue
			}
			if pool.AppendCertsFromPEM(data) {
				loaded++
			}
		}
	}

	// Also load from filesystem (allows overrides/additions)
	if dir != "" {
		fsEntries, err := os.ReadDir(dir)
		if err == nil {
			for _, entry := range fsEntries {
				if entry.IsDir() || (!strings.HasSuffix(entry.Name(), ".crt") && !strings.HasSuffix(entry.Name(), ".pem")) {
					continue
				}
				data, err := os.ReadFile(filepath.Join(dir, entry.Name()))
				if err != nil {
					continue
				}
				if pool.AppendCertsFromPEM(data) {
					loaded++
				}
			}
		}
	}

	if loaded == 0 {
		return nil, fmt.Errorf("no CA certs loaded")
	}
	return pool, nil
}

func (c *Client) Conn() *nats.Conn {
	return c.nc
}

func (c *Client) JS() nats.JetStreamContext {
	return c.js
}
