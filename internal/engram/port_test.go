package engram

import (
	"os/exec"
	"strconv"
	"strings"
	"testing"
)

// TestPortForVignoble verifies that the Go implementation matches the bash
// formula used in bin/pinard:
//
//	printf '%s' "$name" | cksum | awk '{print $1}') % 1000 + 7500
func TestPortForVignoble(t *testing.T) {
	cases := []struct {
		name string
		want int
	}{
		{"misc", 7783},
		{"exohub", 7858},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := PortForVignoble(tc.name)
			if got != tc.want {
				t.Errorf("PortForVignoble(%q) = %d, want %d", tc.name, got, tc.want)
			}
		})
	}
}

// TestPortForVignoble_MatchesBash cross-checks the Go result against the
// actual bash cksum(1) invocation. Skipped when cksum is not available.
func TestPortForVignoble_MatchesBash(t *testing.T) {
	if _, err := exec.LookPath("cksum"); err != nil {
		t.Skip("cksum not available")
	}
	names := []string{"misc", "exohub", "pinard", "test-vigne"}
	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			out, err := exec.Command("bash", "-c",
				`printf '%s' "`+name+`" | cksum | awk '{print $1}'`).Output()
			if err != nil {
				t.Fatalf("bash cksum: %v", err)
			}
			n, err := strconv.ParseUint(strings.TrimSpace(string(out)), 10, 32)
			if err != nil {
				t.Fatalf("parse cksum output %q: %v", string(out), err)
			}
			want := int(n)%1000 + 7500
			got := PortForVignoble(name)
			if got != want {
				t.Errorf("PortForVignoble(%q) = %d, bash formula = %d", name, got, want)
			}
		})
	}
}
