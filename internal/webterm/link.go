package webterm

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"strconv"
	"time"
)

// linkSig computes the HMAC-SHA256 signature over "vignoble|target|exp". The
// vignoble is bound into the signature so one gateway can serve many vignobles
// with a single (per-tenant) secret without cross-vignoble replay.
func linkSig(vignoble, target string, exp int64, secret []byte) string {
	mac := hmac.New(sha256.New, secret)
	fmt.Fprintf(mac, "%s|%s|%d", vignoble, target, exp)
	return hex.EncodeToString(mac.Sum(nil))
}

// BuildLink returns the browser-facing signed URL for a tmux target in a vignoble:
//
//	<baseURL>/sessions?v=<vignoble>&target=<target>&exp=<unix>&sig=<hmac>
//
// baseURL must not have a trailing slash (config.WebtermBaseURL trims it).
func BuildLink(baseURL, vignoble, target string, exp time.Time, secret []byte) string {
	e := exp.Unix()
	q := url.Values{}
	q.Set("v", vignoble)
	q.Set("target", target)
	q.Set("exp", strconv.FormatInt(e, 10))
	q.Set("sig", linkSig(vignoble, target, e, secret))
	return baseURL + "/sessions?" + q.Encode()
}

// BuildUnsignedLink returns the browser URL for a tmux target WITHOUT a signature:
//
//	<baseURL>/sessions?v=<vignoble>&target=<target>
//
// It carries no bearer credential, so it is safe to post publicly (e.g. on an MR):
// the gateway grants access only to a Cognito-authenticated operator of the vignoble
// (see Authorize — isOperator needs no valid link). Use this when auth is enabled;
// use BuildLink for the Phase-1 signed viewer link.
func BuildUnsignedLink(baseURL, vignoble, target string) string {
	q := url.Values{}
	q.Set("v", vignoble)
	q.Set("target", target)
	return baseURL + "/sessions?" + q.Encode()
}

// VerifyLink validates a signed link's signature (constant-time) and expiry.
func VerifyLink(vignoble, target string, exp int64, sig string, secret []byte, now time.Time) error {
	if len(secret) == 0 {
		return fmt.Errorf("no link secret configured")
	}
	want := linkSig(vignoble, target, exp, secret)
	if !hmac.Equal([]byte(want), []byte(sig)) {
		return fmt.Errorf("invalid signature")
	}
	if now.Unix() > exp {
		return fmt.Errorf("link expired")
	}
	return nil
}

// ParseExp parses the exp query value into a unix timestamp.
func ParseExp(s string) (int64, error) {
	return strconv.ParseInt(s, 10, 64)
}
