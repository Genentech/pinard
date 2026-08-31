package webterm

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The gateway must deny a vignoble it does not serve, even with a valid link.
func TestGatewayUnservedVignobleDenied(t *testing.T) {
	gw := &Gateway{Vignobles: []string{"exohub"}, LinkSecret: testSecret, GrantSecret: grantSecret}
	srv := httptest.NewServer(gw.Handler())
	defer srv.Close()

	// A properly-signed link for a vignoble the gateway does NOT serve.
	link := BuildLink(srv.URL, "genomics", "sess", time.Now().Add(time.Hour), testSecret)
	resp, err := http.Get(link)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403 for unserved vignoble, got %d", resp.StatusCode)
	}
}

// A served vignoble with a valid link renders the page (auth disabled).
func TestGatewayServedVignobleAllowed(t *testing.T) {
	gw := &Gateway{Vignobles: []string{"exohub", "genomics"}, LinkSecret: testSecret, GrantSecret: grantSecret}
	srv := httptest.NewServer(gw.Handler())
	defer srv.Close()

	link := BuildLink(srv.URL, "genomics", "sess", time.Now().Add(time.Hour), testSecret)
	resp, err := http.Get(link)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 for served vignoble with valid link, got %d", resp.StatusCode)
	}
}

// With no explicit allowlist, the gateway serves vignobles present in the
// pinard-vignobles KV (self-maintaining) and 403s unknown ones.
func TestGatewayKVDerivedServedSet(t *testing.T) {
	ns, url := startEmbeddedNATSJS(t)
	defer ns.Shutdown()
	client := newJSClient(t, url)
	defer client.Close()
	kv := newKVClient(client)
	if err := PublishOwner(kv, "exohub", "lelongs"); err != nil {
		t.Fatal(err)
	}

	// Empty Vignobles → served set derived from the KV.
	gw := &Gateway{Vignobles: nil, LinkSecret: testSecret, GrantSecret: grantSecret, Owners: &OwnerStore{KV: kv}}
	srv := httptest.NewServer(gw.Handler())
	defer srv.Close()

	// exohub has a published owner → served (valid link → 200).
	ok := BuildLink(srv.URL, "exohub", "sess", time.Now().Add(time.Hour), testSecret)
	if code := getStatus(t, ok); code != http.StatusOK {
		t.Fatalf("exohub (in KV) → %d, want 200", code)
	}
	// genomics not in KV → 403.
	no := BuildLink(srv.URL, "genomics", "sess", time.Now().Add(time.Hour), testSecret)
	if code := getStatus(t, no); code != http.StatusForbidden {
		t.Fatalf("genomics (not in KV) → %d, want 403", code)
	}
}

func getStatus(t *testing.T, url string) int {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

// A responder must ignore a grant scoped to a different vignoble (silent).
func TestResponderIgnoresForeignVignoble(t *testing.T) {
	ns, url := startEmbeddedNATS(t)
	defer ns.Shutdown()
	nc, _ := nats.Connect(url)
	defer nc.Close()

	// Responder serves vignoble "exohub".
	runResponder(t, nc, "exohub", "pinard-nonexistent")

	// Valid grant, but scoped to a different vignoble → responder stays silent.
	foreign, _ := SignGrant(Grant{Vignoble: "genomics", Target: "s", Mode: ModeRO, Exp: time.Now().Add(time.Minute).Unix()}, grantSecret)
	requestViewerExpectSilence(t, nc, "exohub", foreign, "v1")
}
