package webterm

import (
	"testing"
	"time"
)

var grantSecret = []byte("test-grant-secret")

func TestGrantRoundtrip(t *testing.T) {
	now := time.Unix(2_000_000, 0)
	g := Grant{Target: "parcelle--proj-xyz", Mode: ModeRO, Exp: now.Add(time.Minute).Unix()}
	tok, err := SignGrant(g, grantSecret)
	if err != nil {
		t.Fatal(err)
	}
	got, err := VerifyGrant(tok, grantSecret, now)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if got.Target != g.Target || got.Mode != g.Mode || got.Exp != g.Exp {
		t.Fatalf("roundtrip mismatch: %+v vs %+v", got, g)
	}
}

func TestGrantExpired(t *testing.T) {
	now := time.Unix(2_000_000, 0)
	g := Grant{Target: "t", Mode: ModeRO, Exp: now.Add(-time.Second).Unix()}
	tok, _ := SignGrant(g, grantSecret)
	if _, err := VerifyGrant(tok, grantSecret, now); err == nil {
		t.Fatal("expected expiry error")
	}
}

func TestGrantTamperedPayload(t *testing.T) {
	now := time.Unix(2_000_000, 0)
	g := Grant{Target: "t", Mode: ModeRO, Exp: now.Add(time.Minute).Unix()}
	tok, _ := SignGrant(g, grantSecret)
	// Flip the last byte of the base64 payload (before the dot).
	b := []byte(tok)
	for i := 0; i < len(b); i++ {
		if b[i] == '.' {
			if i > 0 {
				b[i-1] ^= 0x01
			}
			break
		}
	}
	if _, err := VerifyGrant(string(b), grantSecret, now); err == nil {
		t.Fatal("expected signature error on tampered payload")
	}
}

func TestGrantWrongSecret(t *testing.T) {
	now := time.Unix(2_000_000, 0)
	g := Grant{Target: "t", Mode: ModeRO, Exp: now.Add(time.Minute).Unix()}
	tok, _ := SignGrant(g, grantSecret)
	if _, err := VerifyGrant(tok, []byte("other"), now); err == nil {
		t.Fatal("expected signature error with wrong secret")
	}
}

func TestGrantMalformed(t *testing.T) {
	now := time.Unix(2_000_000, 0)
	if _, err := VerifyGrant("no-dot-here", grantSecret, now); err == nil {
		t.Fatal("expected malformed error")
	}
}
