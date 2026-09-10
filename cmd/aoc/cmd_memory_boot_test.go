package main

import (
	"strings"
	"testing"
)

func TestNormTitle(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"Database Provenance Migration", "database provenance migration"},
		{"Database Provenance Migration!", "database provenance migration"},
		{"  Multiple   Spaces  ", "multiple spaces"},
		{"hello-world_test", "hello world test"},
		{"", ""},
	}
	for _, c := range cases {
		got := normTitle(c.in)
		if got != c.want {
			t.Errorf("normTitle(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestDedupeBootEntries(t *testing.T) {
	wiki := bootEntry{Scope: "s", Type: "wiki", Title: "Provenance DB Migration", Summary: "Moved to PG.", Ref: "wiki:decisions/provenance-db"}
	entity := bootEntry{Scope: "s", Type: "decision", Title: "Provenance DB Migration", Summary: "Moved to PG.", Ref: "entity:c0f38e655a75abc"}

	t.Run("entity duplicate of wiki is dropped", func(t *testing.T) {
		out := dedupeBootEntries([]bootEntry{wiki, entity})
		if len(out) != 1 {
			t.Fatalf("expected 1 entry, got %d", len(out))
		}
		if out[0].Ref != wiki.Ref {
			t.Errorf("expected wiki ref %q, got %q", wiki.Ref, out[0].Ref)
		}
	})

	t.Run("entity without wiki duplicate is kept", func(t *testing.T) {
		only := bootEntry{Scope: "s", Type: "decision", Title: "Unique Entity", Summary: "Something.", Ref: "entity:abc123"}
		out := dedupeBootEntries([]bootEntry{wiki, only})
		if len(out) != 2 {
			t.Fatalf("expected 2 entries, got %d", len(out))
		}
	})

	t.Run("case and punctuation differences still deduplicated", func(t *testing.T) {
		entityAlt := bootEntry{Scope: "s", Type: "decision", Title: "provenance db migration!", Summary: ".", Ref: "entity:xyz"}
		out := dedupeBootEntries([]bootEntry{wiki, entityAlt})
		if len(out) != 1 {
			t.Fatalf("expected 1 entry after dedupe, got %d", len(out))
		}
		if out[0].Ref != wiki.Ref {
			t.Errorf("wiki ref should be kept, got %q", out[0].Ref)
		}
	})

	t.Run("empty input returns empty", func(t *testing.T) {
		out := dedupeBootEntries(nil)
		if len(out) != 0 {
			t.Errorf("expected empty, got %d entries", len(out))
		}
	})
}

func TestTruncateSummary(t *testing.T) {
	t.Run("short string unchanged", func(t *testing.T) {
		s := "Short summary."
		got := truncateSummary(s, 140)
		if got != s {
			t.Errorf("got %q, want %q", got, s)
		}
	})

	t.Run("long string truncated at word boundary with ellipsis", func(t *testing.T) {
		long := "The worker extension registration flow was broken because the uncorked sandbox bootstrap dropped mandatory dependencies on local Claude settings causing failures."
		got := truncateSummary(long, 50)
		if !strings.HasSuffix(got, "…") {
			t.Errorf("expected ellipsis suffix, got %q", got)
		}
		runes := []rune(got)
		if len(runes) > 51 { // 50 chars + ellipsis
			t.Errorf("result too long: %d runes", len(runes))
		}
		// Must not end with a partial word (last char before ellipsis should be a letter from complete word boundary)
		withoutEllipsis := strings.TrimSuffix(got, "…")
		if strings.HasSuffix(withoutEllipsis, " ") {
			t.Errorf("should not end with trailing space before ellipsis: %q", got)
		}
	})

	t.Run("collapses internal whitespace", func(t *testing.T) {
		s := "hello\n  world\t  foo"
		got := truncateSummary(s, 140)
		if got != "hello world foo" {
			t.Errorf("got %q", got)
		}
	})
}

func TestFormatBootIndex(t *testing.T) {
	entries := []bootEntry{
		{Scope: "my-project", Type: "wiki", Title: "Provenance DB Migration", Summary: "Local SQLite deprecated → PostgreSQL on AWS RDS.", Ref: "wiki:decisions/provenance-db"},
		{Scope: "my-project", Type: "decision", Title: "Use PostgreSQL", Summary: "", Ref: "entity:abc123"},
		{Scope: "__global__", Type: "wiki", Title: "Global Guideline", Summary: "Keep things simple.", Ref: "wiki:guidelines/simple"},
	}

	out := formatBootIndex(entries)

	t.Run("header present", func(t *testing.T) {
		if !strings.Contains(out, "--- Knowledge index") {
			t.Errorf("missing header in output:\n%s", out)
		}
	})

	t.Run("footer present", func(t *testing.T) {
		if !strings.Contains(out, "--- End of knowledge index ---") {
			t.Errorf("missing footer in output:\n%s", out)
		}
	})

	t.Run("scope sections present", func(t *testing.T) {
		if !strings.Contains(out, "[my-project]") {
			t.Errorf("missing scope section [my-project]:\n%s", out)
		}
		if !strings.Contains(out, "[__global__]") {
			t.Errorf("missing scope section [__global__]:\n%s", out)
		}
	})

	t.Run("bullet format with type dash title", func(t *testing.T) {
		if !strings.Contains(out, "• wiki — Provenance DB Migration") {
			t.Errorf("missing bullet entry:\n%s", out)
		}
	})

	t.Run("fetch ref on its own line", func(t *testing.T) {
		lines := strings.Split(out, "\n")
		foundFetch := false
		for _, l := range lines {
			trimmed := strings.TrimSpace(l)
			if trimmed == "fetch: wiki:decisions/provenance-db" {
				foundFetch = true
				break
			}
		}
		if !foundFetch {
			t.Errorf("ref not on its own fetch: line:\n%s", out)
		}
	})

	t.Run("summary shown when non-empty", func(t *testing.T) {
		if !strings.Contains(out, "Local SQLite deprecated") {
			t.Errorf("missing summary text:\n%s", out)
		}
	})

	t.Run("no summary line when empty", func(t *testing.T) {
		lines := strings.Split(out, "\n")
		for i, l := range lines {
			if strings.Contains(l, "• decision — Use PostgreSQL") {
				// Next non-empty line should be the fetch: line, not a summary
				for _, next := range lines[i+1:] {
					if strings.TrimSpace(next) == "" {
						continue
					}
					if !strings.HasPrefix(strings.TrimSpace(next), "fetch:") {
						t.Errorf("expected fetch: line after title with no summary, got: %q", next)
					}
					break
				}
			}
		}
	})

	t.Run("ref never appears mid-line with other content", func(t *testing.T) {
		lines := strings.Split(out, "\n")
		for _, l := range lines {
			trimmed := strings.TrimSpace(l)
			if strings.HasPrefix(trimmed, "fetch:") {
				// A fetch: line must contain only the ref, nothing else substantive
				parts := strings.SplitN(trimmed, " ", 2)
				if len(parts) != 2 || strings.Contains(parts[1], "·") {
					t.Errorf("fetch line has unexpected format: %q", l)
				}
			}
		}
	})
}
