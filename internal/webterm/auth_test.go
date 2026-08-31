package webterm

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	jose "github.com/go-jose/go-jose/v4"
)

// newMockOIDC serves an OIDC discovery doc + JWKS and returns a token minter.
func newMockOIDC(t *testing.T) (issuer string, mint func(map[string]any) string, closeFn func()) {
	t.Helper()
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	const kid = "test-key"
	jwks := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
		{Key: &priv.PublicKey, KeyID: kid, Algorithm: "RS256", Use: "sig"},
	}}

	mux := http.NewServeMux()
	var iss string
	mux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer":                                iss,
			"authorization_endpoint":                iss + "/authorize",
			"token_endpoint":                        iss + "/token",
			"jwks_uri":                              iss + "/jwks",
			"id_token_signing_alg_values_supported": []string{"RS256"},
		})
	})
	mux.HandleFunc("/jwks", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(jwks)
	})
	ts := httptest.NewServer(mux)
	iss = ts.URL

	signer, err := jose.NewSigner(
		jose.SigningKey{Algorithm: jose.RS256, Key: priv},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", kid),
	)
	if err != nil {
		t.Fatal(err)
	}
	mint = func(claims map[string]any) string {
		if _, ok := claims["iss"]; !ok {
			claims["iss"] = iss
		}
		payload, _ := json.Marshal(claims)
		obj, err := signer.Sign(payload)
		if err != nil {
			t.Fatal(err)
		}
		s, err := obj.CompactSerialize()
		if err != nil {
			t.Fatal(err)
		}
		return s
	}
	return iss, mint, ts.Close
}

func TestSessionCookieRoundtrip(t *testing.T) {
	a := &Authenticator{cookieSecret: []byte("cookie-secret"), sessionTTL: time.Hour}
	now := time.Unix(3_000_000, 0)

	tok, err := a.signSession(Identity{Username: "lelongs", Email: "x@y.com"}, now)
	if err != nil {
		t.Fatal(err)
	}
	id, err := a.verifySession(tok, now)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if id.Username != "lelongs" || id.Email != "x@y.com" {
		t.Fatalf("roundtrip mismatch: %+v", id)
	}

	// Expired
	old, _ := a.signSession(Identity{Username: "u"}, now.Add(-2*time.Hour))
	if _, err := a.verifySession(old, now); err == nil {
		t.Fatal("expected expiry error")
	}
	// Tampered
	if _, err := a.verifySession(tok+"x", now); err == nil {
		t.Fatal("expected signature error on tampered cookie")
	}
	// Wrong secret
	a2 := &Authenticator{cookieSecret: []byte("other"), sessionTTL: time.Hour}
	if _, err := a2.verifySession(tok, now); err == nil {
		t.Fatal("expected signature error with wrong secret")
	}
}

func TestIDTokenVerification(t *testing.T) {
	issuer, mint, closeFn := newMockOIDC(t)
	defer closeFn()

	a, err := NewAuthenticator(context.Background(), issuer, "client-123", issuer+"/cb", []string{"openid"}, []byte("cookie"), time.Hour, false)
	if err != nil {
		t.Fatalf("authenticator: %v", err)
	}
	now := time.Now()

	good := mint(map[string]any{"aud": "client-123", "exp": now.Add(time.Hour).Unix(), "iat": now.Unix(), "sub": "u", "preferred_username": "lelongs", "token_use": "id"})
	if _, err := a.verifier.Verify(context.Background(), good); err != nil {
		t.Fatalf("valid token rejected: %v", err)
	}

	wrongAud := mint(map[string]any{"aud": "other", "exp": now.Add(time.Hour).Unix(), "iat": now.Unix(), "sub": "u"})
	if _, err := a.verifier.Verify(context.Background(), wrongAud); err == nil {
		t.Fatal("expected aud mismatch failure")
	}

	expired := mint(map[string]any{"aud": "client-123", "exp": now.Add(-time.Hour).Unix(), "iat": now.Add(-2 * time.Hour).Unix(), "sub": "u"})
	if _, err := a.verifier.Verify(context.Background(), expired); err == nil {
		t.Fatal("expected expiry failure")
	}

	// Tampered signature (flip a char in the signature segment).
	bad := []byte(good)
	bad[len(bad)-1] ^= 0x01
	if _, err := a.verifier.Verify(context.Background(), string(bad)); err == nil {
		t.Fatal("expected signature failure on tampered token")
	}
}

func TestCustomCallbackPath(t *testing.T) {
	issuer, _, closeFn := newMockOIDC(t)
	defer closeFn()
	a, err := NewAuthenticator(context.Background(), issuer, "client-123",
		"https://pinard.example.com/api/oauth2-redirect",
		[]string{"openid"}, []byte("cookie"), time.Hour, false)
	if err != nil {
		t.Fatal(err)
	}
	if a.CallbackPath() != "/api/oauth2-redirect" {
		t.Fatalf("callback path = %q, want /api/oauth2-redirect", a.CallbackPath())
	}

	gw := &Gateway{Vignobles: []string{"test"}, LinkSecret: testSecret, GrantSecret: grantSecret, Auth: a}
	srv := httptest.NewServer(gw.Handler())
	defer srv.Close()

	// The custom callback path is served by the gateway (no flow cookie → 400
	// "missing flow state", NOT a 404 — proving the handler is wired there).
	resp, err := http.Get(srv.URL + "/api/oauth2-redirect?code=x&state=y")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		t.Fatal("custom callback path not served (404)")
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 missing flow state, got %d", resp.StatusCode)
	}
}

func TestGatewayRedirectsUnauthenticated(t *testing.T) {
	issuer, _, closeFn := newMockOIDC(t)
	defer closeFn()
	a, err := NewAuthenticator(context.Background(), issuer, "client-123", "https://x/cb", []string{"openid"}, []byte("cookie"), time.Hour, false)
	if err != nil {
		t.Fatal(err)
	}
	gw := &Gateway{Vignobles: []string{"test"}, LinkSecret: testSecret, GrantSecret: grantSecret, Auth: a}
	srv := httptest.NewServer(gw.Handler())
	defer srv.Close()

	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Get(srv.URL + "/sessions?target=foo")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusFound {
		t.Fatalf("expected 302 redirect to login, got %d", resp.StatusCode)
	}
	if loc := resp.Header.Get("Location"); !strings.Contains(loc, issuer+"/authorize") {
		t.Fatalf("expected redirect to authorize endpoint, got %q", loc)
	}
}
