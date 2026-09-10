package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

// bootEntry mirrors the {scope, type, title, summary, ref} manifest shape
// returned by the boot-recall server (v2).
type bootEntry struct {
	Scope   string `json:"scope"`
	Type    string `json:"type"`
	Title   string `json:"title"`
	Summary string `json:"summary"`
	Ref     string `json:"ref"`
}

type bootRecallResponse struct {
	Entries []bootEntry `json:"entries"`
	Meta    struct {
		TotalEntries int `json:"total_entries"`
	} `json:"meta"`
}

var memoryBootContextCmd = &cobra.Command{
	Use:   "memory-boot-context",
	Short: "Fetch hierarchical boot knowledge and print it as a labeled context block",
	Long: `Sends a boot-recall request to the pinard memory service over NATS and
prints the result as a static, scope-labeled text block suitable for injection
into an agent's initial context. Fail-open: prints nothing and exits 0 on any
error or timeout.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		vignoble, _ := cmd.Flags().GetString("vignoble")
		groupID, _ := cmd.Flags().GetString("group-id")
		taskText, _ := cmd.Flags().GetString("task")
		timeoutMs, _ := cmd.Flags().GetInt("timeout")
		topK, _ := cmd.Flags().GetInt("top-k")

		if vignoble == "" || groupID == "" {
			// Missing required info — fail-open, print nothing.
			return nil
		}

		creds, err := config.LoadCredentials()
		if err != nil {
			// Fail-open: no creds → no boot context.
			return nil
		}

		nc := pnats.NewClient(creds)
		defer nc.Close()

		scopes := []string{"__global__", "vignoble-" + vignoble, groupID}

		payload := map[string]any{
			"scopes":   scopes,
			"group_id": groupID,
			"vignoble": vignoble,
			"task_text": taskText,
			"top_k":    topK,
		}

		subject := pnats.BootRecallSubject(vignoble)
		timeout := time.Duration(timeoutMs) * time.Millisecond

		msg, err := nc.Request(subject, payload, timeout)
		if err != nil {
			// Timeout or no responder — fail-open, print nothing.
			return nil
		}

		var resp bootRecallResponse
		if err := json.Unmarshal(msg.Data, &resp); err != nil {
			return nil
		}

		if len(resp.Entries) == 0 {
			return nil
		}

		deduped := dedupeBootEntries(resp.Entries)
		fmt.Print(formatBootIndex(deduped))
		return nil
	},
}

func init() {
	memoryBootContextCmd.Flags().String("vignoble", "", "Vignoble name (NATS namespace)")
	memoryBootContextCmd.Flags().String("group-id", "", "Vigne/group ID (innermost scope)")
	memoryBootContextCmd.Flags().String("task", "", "Task text for query shaping (optional)")
	memoryBootContextCmd.Flags().Int("timeout", 5000, "Request timeout in milliseconds")
	memoryBootContextCmd.Flags().Int("top-k", 5, "Maximum index entries per scope")
	rootCmd.AddCommand(memoryBootContextCmd)
}

// normTitle returns a normalized version of a title for deduplication:
// lowercase, all non-alphanumeric runs collapsed to a single space, trimmed.
var _nonAlnum = regexp.MustCompile(`[^\p{L}\p{N}]+`)

func normTitle(s string) string {
	return strings.TrimSpace(_nonAlnum.ReplaceAllString(strings.ToLower(s), " "))
}

// dedupeBootEntries removes cross-type duplicate entries where a wiki entry
// (slug ref) and an entity entry (hash ref) share the same normalized title.
// The wiki entry is preferred; the entity duplicate is dropped.
func dedupeBootEntries(entries []bootEntry) []bootEntry {
	// First pass: collect all normalized titles that have a wiki entry.
	wikiTitles := map[string]struct{}{}
	for _, e := range entries {
		if strings.HasPrefix(e.Ref, "wiki:") {
			wikiTitles[normTitle(e.Title)] = struct{}{}
		}
	}
	// Second pass: drop entity entries whose title matches a wiki entry.
	out := entries[:0:0]
	for _, e := range entries {
		if !strings.HasPrefix(e.Ref, "wiki:") {
			if _, dup := wikiTitles[normTitle(e.Title)]; dup {
				continue
			}
		}
		out = append(out, e)
	}
	return out
}

// truncateSummary collapses internal whitespace and truncates s to at most
// maxChars on a word boundary, appending "…" if truncated.
func truncateSummary(s string, maxChars int) string {
	s = strings.Join(strings.Fields(s), " ")
	if len([]rune(s)) <= maxChars {
		return s
	}
	runes := []rune(s)
	// Walk back from maxChars to find a word boundary.
	i := maxChars
	for i > 0 && !unicode.IsSpace(runes[i]) {
		i--
	}
	if i == 0 {
		i = maxChars
	}
	return strings.TrimRight(string(runes[:i]), " ") + "…"
}

// formatBootIndex renders entries in a multi-line bullet format:
//
//	--- Knowledge index (expand any item with: recall fetch=<ref>) ---
//
//	[scope]
//
//	  • type — Title
//	    Summary text here.
//	    fetch: ref
func formatBootIndex(entries []bootEntry) string {
	var sb strings.Builder
	sb.WriteString("--- Knowledge index (expand any item with: recall fetch=<ref>) ---\n")

	type section struct {
		scope   string
		entries []bootEntry
	}
	var sections []section
	scopeIndex := map[string]int{}
	for _, e := range entries {
		if idx, ok := scopeIndex[e.Scope]; ok {
			sections[idx].entries = append(sections[idx].entries, e)
		} else {
			scopeIndex[e.Scope] = len(sections)
			sections = append(sections, section{scope: e.Scope, entries: []bootEntry{e}})
		}
	}

	for _, sec := range sections {
		fmt.Fprintf(&sb, "\n[%s]\n", sec.scope)
		for _, e := range sec.entries {
			fmt.Fprintf(&sb, "\n  \u2022 %s \u2014 %s\n", e.Type, e.Title)
			if e.Summary != "" {
				summary := truncateSummary(e.Summary, 140)
				fmt.Fprintf(&sb, "    %s\n", summary)
			}
			fmt.Fprintf(&sb, "    fetch: %s\n", e.Ref)
		}
	}

	sb.WriteString("\n--- End of knowledge index ---\n")
	return sb.String()
}
