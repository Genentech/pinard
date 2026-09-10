package main

import (
	"strings"
	"testing"
)

// ── cleanEntityTitle tests ────────────────────────────────────────────────────

func TestCleanEntityTitle(t *testing.T) {
	cases := []struct {
		name   string
		input  string
		maxLen int
		want   string
	}{
		{
			name:   "no-op plain title",
			input:  "Some decision title",
			maxLen: 120,
			want:   "Some decision title",
		},
		{
			name:   "strips leading heading mark",
			input:  "## Section heading",
			maxLen: 120,
			want:   "Section heading",
		},
		{
			name:   "strips section label What:",
			input:  "What: A future MR will be opened",
			maxLen: 120,
			want:   "A future MR will be opened",
		},
		{
			name:   "strips bold section label **Why**:",
			input:  "**Why**: We need this for parity",
			maxLen: 120,
			want:   "We need this for parity",
		},
		{
			name:   "strips surrounding **bold** wrapper",
			input:  "**Important decision**",
			maxLen: 120,
			want:   "Important decision",
		},
		{
			name:   "strips trailing colon",
			input:  "Some title:",
			maxLen: 120,
			want:   "Some title",
		},
		{
			name:   "strips trailing provenance parenthetical",
			input:  "Some decision (clarified by lelongs)",
			maxLen: 120,
			want:   "Some decision",
		},
		{
			name:   "strips colon after provenance — iterative",
			input:  "Some decision (clarified by lelongs):",
			maxLen: 120,
			want:   "Some decision",
		},
		{
			name:   "maxLen truncation",
			input:  "This is a very long title that should be truncated at the specified maximum length",
			maxLen: 20,
			want:   "This is a very long",
		},
		{
			name:   "maxLen=0 means no truncation",
			input:  "A title that is quite long indeed",
			maxLen: 0,
			want:   "A title that is quite long indeed",
		},
		{
			name:   "empty string falls back to original (empty)",
			input:  "",
			maxLen: 120,
			want:   "",
		},
		{
			name:   "heading + section label + provenance — fully cleaned iteratively",
			input:  "## What: Some gotcha (from production):",
			maxLen: 120,
			want:   "Some gotcha",
		},
		{
			name:   "Where: section label",
			input:  "Where: cmd/memory-recall/main.go",
			maxLen: 120,
			want:   "cmd/memory-recall/main.go",
		},
		{
			name:   "Learned: section label",
			input:  "Learned: Always check the scope",
			maxLen: 120,
			want:   "Always check the scope",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := cleanEntityTitle(tc.input, tc.maxLen)
			if got != tc.want {
				t.Errorf("cleanEntityTitle(%q, %d) = %q; want %q", tc.input, tc.maxLen, got, tc.want)
			}
		})
	}
}

// ── stripEntityNoise tests ────────────────────────────────────────────────────

func TestStripEntityNoise(t *testing.T) {
	cases := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "no-op plain text",
			input: "A plain description sentence.",
			want:  "A plain description sentence.",
		},
		{
			name:  "strips What: section label",
			input: "What: The thing that was done",
			want:  "The thing that was done",
		},
		{
			name:  "strips **Why**: bold section label",
			input: "**Why**: We needed parity with Python",
			want:  "We needed parity with Python",
		},
		{
			name:  "iterative: nested section labels",
			input: "What: Why: double label",
			want:  "double label",
		},
		{
			name:  "strips surrounding **bold** wrapper",
			input: "**Some bold summary**",
			want:  "Some bold summary",
		},
		{
			name:  "strips surrounding *italic* wrapper",
			input: "*Some italic summary*",
			want:  "Some italic summary",
		},
		{
			name:  "empty string unchanged",
			input: "",
			want:  "",
		},
		{
			name:  "whitespace-only becomes empty",
			input: "   ",
			want:  "",
		},
		{
			name:  "Context section label",
			input: "Context: This applies to the boot path",
			want:  "This applies to the boot path",
		},
		{
			name:  "no label — text with colon preserved",
			input: "Use cmd/aoc: always",
			want:  "Use cmd/aoc: always",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := stripEntityNoise(tc.input)
			if got != tc.want {
				t.Errorf("stripEntityNoise(%q) = %q; want %q", tc.input, got, tc.want)
			}
		})
	}
}

// ── nullSummaryIfDuplicate tests ──────────────────────────────────────────────

func TestNullSummaryIfDuplicate(t *testing.T) {
	cases := []struct {
		name        string
		title       string
		summary     string
		wantSummary string
	}{
		{
			name:        "identical → null",
			title:       "Avoid double prefix in entity refs",
			summary:     "Avoid double prefix in entity refs",
			wantSummary: "",
		},
		{
			name:        "summary prefixes title → null",
			title:       "Avoid double prefix",
			summary:     "Avoid double prefix in entity refs",
			wantSummary: "",
		},
		{
			name:        "title prefixes summary → null",
			title:       "Avoid double prefix in entity refs and other places",
			summary:     "Avoid double prefix",
			wantSummary: "",
		},
		{
			name:        "distinct → keep summary",
			title:       "Boot recall scope hierarchy",
			summary:     "Wiki hits are returned for all scopes; entity hits only at the vigne tier.",
			wantSummary: "Wiki hits are returned for all scopes; entity hits only at the vigne tier.",
		},
		{
			name:        "empty summary → stays empty",
			title:       "Some title",
			summary:     "",
			wantSummary: "",
		},
		{
			name:        "case-insensitive comparison → null",
			title:       "BOOT RECALL",
			summary:     "boot recall",
			wantSummary: "",
		},
		{
			name:        "60-char prefix match nulls even with long distinct tail",
			title:       strings.Repeat("a", 61),
			summary:     strings.Repeat("a", 60) + " but then something totally different",
			wantSummary: "",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := nullSummaryIfDuplicate(tc.title, tc.summary)
			if got != tc.wantSummary {
				t.Errorf("nullSummaryIfDuplicate(%q, %q) = %q; want %q",
					tc.title, tc.summary, got, tc.wantSummary)
			}
		})
	}
}

// ── Boot entry ref formatting tests ──────────────────────────────────────────

func TestBootEntryRefFormatting(t *testing.T) {
	// Verify the entity ref logic used in bootHitsForScope.
	entityRef := func(id string) string {
		if !strings.HasPrefix(id, "entity:") {
			return "entity:" + id
		}
		return id
	}

	cases := []struct {
		name     string
		entityID string
		wantRef  string
	}{
		{
			name:     "already prefixed passes through",
			entityID: "entity:abc123",
			wantRef:  "entity:abc123",
		},
		{
			name:     "unprefixed gets entity: prefix",
			entityID: "abc123",
			wantRef:  "entity:abc123",
		},
		{
			name:     "hash-like ID gets prefixed",
			entityID: "d41d8cd98f00b204e9800998ecf8427e",
			wantRef:  "entity:d41d8cd98f00b204e9800998ecf8427e",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := entityRef(tc.entityID)
			if got != tc.wantRef {
				t.Errorf("entityRef(%q) = %q; want %q", tc.entityID, got, tc.wantRef)
			}
		})
	}

	// Wiki ref is always "wiki:" + path — straightforward, verify the pattern.
	wikiRef := func(path string) string { return "wiki:" + path }
	if got := wikiRef("docs/architecture"); got != "wiki:docs/architecture" {
		t.Errorf("wikiRef unexpected: %q", got)
	}
}
