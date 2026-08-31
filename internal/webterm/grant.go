package webterm

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Grant is the short-lived, gateway-issued authorization the responder verifies
// before attaching. The gateway is the sole SSO/authz boundary; the grant proves
// to the responder that a request originated from the gateway (anyone with NATS
// creds could otherwise publish to the request subject).
type Grant struct {
	Vignoble string `json:"vignoble"` // namespace the grant is scoped to
	Target   string `json:"target"`
	Mode     string `json:"mode"` // ModeRO / ModeRW
	Exp      int64  `json:"exp"`  // unix seconds
}

// SignGrant serializes and signs a grant: "<b64url(json)>.<hex(hmac)>".
func SignGrant(g Grant, secret []byte) (string, error) {
	if len(secret) == 0 {
		return "", fmt.Errorf("no grant secret configured")
	}
	payload, err := json.Marshal(g)
	if err != nil {
		return "", err
	}
	b64 := base64.RawURLEncoding.EncodeToString(payload)
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(b64))
	sig := hex.EncodeToString(mac.Sum(nil))
	return b64 + "." + sig, nil
}

// VerifyGrant validates a grant token's signature (constant-time) and expiry and
// returns the decoded grant.
func VerifyGrant(token string, secret []byte, now time.Time) (Grant, error) {
	var g Grant
	if len(secret) == 0 {
		return g, fmt.Errorf("no grant secret configured")
	}
	b64, sig, ok := strings.Cut(token, ".")
	if !ok {
		return g, fmt.Errorf("malformed grant")
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(b64))
	want := hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(want), []byte(sig)) {
		return g, fmt.Errorf("invalid grant signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(b64)
	if err != nil {
		return g, fmt.Errorf("invalid grant payload: %w", err)
	}
	if err := json.Unmarshal(payload, &g); err != nil {
		return g, fmt.Errorf("invalid grant json: %w", err)
	}
	if now.Unix() > g.Exp {
		return g, fmt.Errorf("grant expired")
	}
	return g, nil
}
