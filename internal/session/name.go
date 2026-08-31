package session

import "strings"

// SanitizeName makes a string safe to use as a tmux target.
//
// tmux uses `.` and `:` as target separators (`session:window.pane`), so a
// session name containing either cannot be addressed reliably. Whitespace is
// also replaced because unquoted names break `-t` targeting. The mapping is
// deterministic and idempotent (its output contains none of the replaced
// characters), so callers may apply it more than once safely.
func SanitizeName(s string) string {
	return strings.Map(func(r rune) rune {
		switch r {
		case '.', ':', ' ', '\t', '\n', '\r':
			return '-'
		}
		return r
	}, s)
}
