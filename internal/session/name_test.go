package session

import "testing"

func TestSanitizeName(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"semantic-search--exo-cli-42abc", "semantic-search--exo-cli-42abc"}, // already safe: unchanged
		{"a.b.c", "a-b-c"},           // dots -> dashes
		{"a:b", "a-b"},               // colons -> dashes
		{"with space", "with-space"}, // whitespace -> dash
		{"tab\tsep", "tab-sep"},
		{"v1.2:x y", "v1-2-x-y"},
		{"", ""},
		{"UPPER_lower-09", "UPPER_lower-09"}, // preserved chars
	}
	for _, c := range cases {
		if got := SanitizeName(c.in); got != c.want {
			t.Errorf("SanitizeName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSanitizeNameIdempotent(t *testing.T) {
	for _, in := range []string{"a.b:c d", "issues", "x..y::z", "already-safe"} {
		once := SanitizeName(in)
		twice := SanitizeName(once)
		if once != twice {
			t.Errorf("SanitizeName not idempotent for %q: %q != %q", in, once, twice)
		}
		for _, r := range once {
			if r == '.' || r == ':' || r == ' ' || r == '\t' {
				t.Errorf("SanitizeName(%q) = %q still contains a forbidden tmux char", in, once)
			}
		}
	}
}

func TestIsReservedWindow(t *testing.T) {
	if !IsReservedWindow(RegisseurWindow) {
		t.Errorf("RegisseurWindow %q must be reserved", RegisseurWindow)
	}
	if IsReservedWindow("cerebro-workers") {
		t.Error("a normal parcelle must not be reserved")
	}
	if !IsReservedWindow("[régisseur]") {
		t.Error("literal reserved name should match")
	}
}
