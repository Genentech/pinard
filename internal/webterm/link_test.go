package webterm

import (
	"net/url"
	"strings"
	"testing"
	"time"
)

var testSecret = []byte("test-link-secret")

const testVig = "exohub"

func TestBuildLinkVerifyRoundtrip(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(time.Hour)
	link := BuildLink("https://pinard.example", testVig, "myparcelle--proj-abc123", exp, testSecret)

	if !strings.HasPrefix(link, "https://pinard.example/sessions?") {
		t.Fatalf("unexpected link prefix: %s", link)
	}
	u, err := url.Parse(link)
	if err != nil {
		t.Fatal(err)
	}
	q := u.Query()
	if q.Get("v") != testVig {
		t.Fatalf("missing v=%s in link: %s", testVig, link)
	}
	expUnix, err := ParseExp(q.Get("exp"))
	if err != nil {
		t.Fatal(err)
	}
	if err := VerifyLink(q.Get("v"), q.Get("target"), expUnix, q.Get("sig"), testSecret, now); err != nil {
		t.Fatalf("verify failed: %v", err)
	}
}

func TestVerifyLinkExpired(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(-time.Second)
	sig := linkSig(testVig, "t", exp.Unix(), testSecret)
	if err := VerifyLink(testVig, "t", exp.Unix(), sig, testSecret, now); err == nil {
		t.Fatal("expected expiry error")
	}
}

func TestVerifyLinkTamperedTarget(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(time.Hour)
	sig := linkSig(testVig, "original", exp.Unix(), testSecret)
	if err := VerifyLink(testVig, "tampered", exp.Unix(), sig, testSecret, now); err == nil {
		t.Fatal("expected signature error on tampered target")
	}
}

func TestVerifyLinkTamperedVignoble(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(time.Hour)
	sig := linkSig("exohub", "t", exp.Unix(), testSecret)
	if err := VerifyLink("genomics", "t", exp.Unix(), sig, testSecret, now); err == nil {
		t.Fatal("expected signature error on altered vignoble (cross-vignoble replay)")
	}
}

func TestVerifyLinkTamperedSig(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(time.Hour)
	if err := VerifyLink(testVig, "t", exp.Unix(), "deadbeef", testSecret, now); err == nil {
		t.Fatal("expected signature error on bad sig")
	}
}

func TestVerifyLinkWrongSecret(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	exp := now.Add(time.Hour)
	sig := linkSig(testVig, "t", exp.Unix(), testSecret)
	if err := VerifyLink(testVig, "t", exp.Unix(), sig, []byte("other-secret"), now); err == nil {
		t.Fatal("expected signature error with wrong secret")
	}
}

func TestVerifyLinkNoSecret(t *testing.T) {
	if err := VerifyLink(testVig, "t", time.Now().Add(time.Hour).Unix(), "x", nil, time.Now()); err == nil {
		t.Fatal("expected error with no secret")
	}
}
