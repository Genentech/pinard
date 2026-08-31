package webterm

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

const (
	sessionCookie = "pinard_webterm_session"
	flowCookie    = "pinard_webterm_flow"
	flowMaxAge    = 10 * time.Minute
)

// Authenticator runs the in-gateway OIDC login flow (Authorization Code
// + PKCE, public client — no secret) and issues/validates a stateless signed
// session cookie. nil when auth is disabled (Phase-1 fallback).
type Authenticator struct {
	verifier     *oidc.IDTokenVerifier
	oauth2       oauth2.Config
	cookieSecret []byte
	sessionTTL   time.Duration
	secure       bool   // Secure flag on cookies (base_url is https)
	callbackPath string // URL path of the redirect_uri (where the OIDC provider sends the code)
}

// NewAuthenticator discovers the OIDC endpoints from issuer and builds the flow.
// The redirect_uri path is parsed from redirectURL so the gateway serves the
// callback at exactly the path the OIDC provider redirects to (whatever was registered).
func NewAuthenticator(ctx context.Context, issuer, clientID, redirectURL string, scopes []string, cookieSecret []byte, sessionTTL time.Duration, secure bool) (*Authenticator, error) {
	if len(cookieSecret) == 0 {
		return nil, fmt.Errorf("webterm auth: cookie secret required")
	}
	provider, err := oidc.NewProvider(ctx, issuer)
	if err != nil {
		return nil, fmt.Errorf("oidc discovery (%s): %w", issuer, err)
	}
	cbPath := "/sessions/auth/callback"
	if u, err := url.Parse(redirectURL); err == nil && u.Path != "" {
		cbPath = u.Path
	}
	return &Authenticator{
		verifier: provider.Verifier(&oidc.Config{ClientID: clientID}),
		oauth2: oauth2.Config{
			ClientID:    clientID,
			Endpoint:    provider.Endpoint(),
			RedirectURL: redirectURL,
			Scopes:      scopes,
		},
		cookieSecret: cookieSecret,
		sessionTTL:   sessionTTL,
		secure:       secure,
		callbackPath: cbPath,
	}, nil
}

// CallbackPath is the URL path the OIDC provider redirects to (from the configured
// redirect_uri). The gateway registers the callback handler here.
func (a *Authenticator) CallbackPath() string { return a.callbackPath }

// ── Signed cookie payloads ────────────────────────────────────

func (a *Authenticator) sign(b64 string) string {
	mac := hmac.New(sha256.New, a.cookieSecret)
	mac.Write([]byte(b64))
	return hex.EncodeToString(mac.Sum(nil))
}

func (a *Authenticator) seal(v any) (string, error) {
	payload, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	b64 := base64.RawURLEncoding.EncodeToString(payload)
	return b64 + "." + a.sign(b64), nil
}

func (a *Authenticator) open(token string, v any) error {
	b64, sig, ok := strings.Cut(token, ".")
	if !ok {
		return fmt.Errorf("malformed cookie")
	}
	if !hmac.Equal([]byte(a.sign(b64)), []byte(sig)) {
		return fmt.Errorf("bad signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(b64)
	if err != nil {
		return err
	}
	return json.Unmarshal(payload, v)
}

type sessionData struct {
	Username string `json:"u"`
	Email    string `json:"e"`
	Exp      int64  `json:"x"`
}

type flowData struct {
	State    string `json:"s"`
	Verifier string `json:"v"`
	ReturnTo string `json:"r"`
	Exp      int64  `json:"x"`
}

// signSession/verifySession are the session-cookie codec (exported-ish for tests).
func (a *Authenticator) signSession(id Identity, now time.Time) (string, error) {
	return a.seal(sessionData{Username: id.Username, Email: id.Email, Exp: now.Add(a.sessionTTL).Unix()})
}

func (a *Authenticator) verifySession(raw string, now time.Time) (Identity, error) {
	var s sessionData
	if err := a.open(raw, &s); err != nil {
		return Identity{}, err
	}
	if now.Unix() > s.Exp {
		return Identity{}, fmt.Errorf("session expired")
	}
	return Identity{Username: s.Username, Email: s.Email}, nil
}

// Identify returns the authenticated identity from the session cookie.
func (a *Authenticator) Identify(r *http.Request) (Identity, bool) {
	c, err := r.Cookie(sessionCookie)
	if err != nil {
		return Identity{}, false
	}
	id, err := a.verifySession(c.Value, time.Now())
	if err != nil {
		return Identity{}, false
	}
	return id, true
}

// ── Login flow ────────────────────────────────────────────────

func randToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// StartLogin redirects the browser to the OIDC provider, stashing state + PKCE verifier +
// the post-login return path in a short-lived signed flow cookie.
func (a *Authenticator) StartLogin(w http.ResponseWriter, r *http.Request, returnTo string) {
	state := randToken()
	verifier := oauth2.GenerateVerifier()
	sealed, err := a.seal(flowData{State: state, Verifier: verifier, ReturnTo: safeReturnTo(returnTo), Exp: time.Now().Add(flowMaxAge).Unix()})
	if err != nil {
		http.Error(w, "auth error", http.StatusInternalServerError)
		return
	}
	http.SetCookie(w, a.cookie(flowCookie, sealed, int(flowMaxAge.Seconds())))
	url := a.oauth2.AuthCodeURL(state, oauth2.S256ChallengeOption(verifier))
	http.Redirect(w, r, url, http.StatusFound)
}

// HandleCallback completes the flow: verifies state, exchanges the code with the
// PKCE verifier, validates the ID token, and sets the session cookie.
func (a *Authenticator) HandleCallback(w http.ResponseWriter, r *http.Request) {
	c, err := r.Cookie(flowCookie)
	if err != nil {
		http.Error(w, "auth: missing flow state", http.StatusBadRequest)
		return
	}
	var fd flowData
	if err := a.open(c.Value, &fd); err != nil || time.Now().Unix() > fd.Exp {
		http.Error(w, "auth: invalid flow state", http.StatusBadRequest)
		return
	}
	if r.URL.Query().Get("state") != fd.State {
		http.Error(w, "auth: state mismatch", http.StatusBadRequest)
		return
	}
	code := r.URL.Query().Get("code")
	if code == "" {
		http.Error(w, "auth: no code", http.StatusBadRequest)
		return
	}

	token, err := a.oauth2.Exchange(r.Context(), code, oauth2.VerifierOption(fd.Verifier))
	if err != nil {
		http.Error(w, "auth: code exchange failed", http.StatusUnauthorized)
		return
	}
	rawID, ok := token.Extra("id_token").(string)
	if !ok {
		http.Error(w, "auth: no id_token", http.StatusUnauthorized)
		return
	}
	idToken, err := a.verifier.Verify(r.Context(), rawID)
	if err != nil {
		http.Error(w, "auth: invalid id_token", http.StatusUnauthorized)
		return
	}
	var claims struct {
		PreferredUsername string `json:"preferred_username"`
		Email             string `json:"email"`
		Sub               string `json:"sub"`
	}
	if err := idToken.Claims(&claims); err != nil {
		http.Error(w, "auth: bad claims", http.StatusUnauthorized)
		return
	}
	// Resolve username from standard OIDC claims: preferred_username → sub.
	username := claims.PreferredUsername
	if username == "" {
		username = claims.Sub
	}
	if username == "" {
		http.Error(w, "auth: no username claim", http.StatusUnauthorized)
		return
	}

	sealed, err := a.signSession(Identity{Username: username, Email: claims.Email}, time.Now())
	if err != nil {
		http.Error(w, "auth error", http.StatusInternalServerError)
		return
	}
	http.SetCookie(w, a.cookie(sessionCookie, sealed, int(a.sessionTTL.Seconds())))
	http.SetCookie(w, a.clearCookie(flowCookie))
	http.Redirect(w, r, fd.ReturnTo, http.StatusFound)
}

func (a *Authenticator) cookie(name, value string, maxAge int) *http.Cookie {
	return &http.Cookie{
		Name:  name,
		Value: value,
		// Path "/" so the flow cookie reaches the callback even when it lives
		// outside /sessions (e.g. a registered /api/oauth2-redirect), and the
		// session cookie is sent to both.
		Path:     "/",
		MaxAge:   maxAge,
		HttpOnly: true,
		Secure:   a.secure,
		SameSite: http.SameSiteLaxMode,
	}
}

func (a *Authenticator) clearCookie(name string) *http.Cookie {
	c := a.cookie(name, "", -1)
	return c
}

// safeReturnTo prevents open redirects: only same-site absolute paths are kept.
func safeReturnTo(p string) string {
	if strings.HasPrefix(p, "/") && !strings.HasPrefix(p, "//") {
		return p
	}
	return "/sessions"
}
