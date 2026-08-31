package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

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

		// Format as a compact, scope-grouped manifest.
		// Each entry: type · title · summary · ref
		// Drill into any entry with: recall(fetch=<ref>)
		var sb strings.Builder
		sb.WriteString("--- Knowledge index for your scope/task (use recall fetch=<ref> to expand) ---\n")

		// Group entries by scope to keep sections contiguous.
		type section struct {
			scope   string
			entries []bootEntry
		}
		var sections []section
		scopeIndex := map[string]int{}
		for _, e := range resp.Entries {
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
				var line string
				if e.Summary == "" || e.Summary == e.Title {
					line = fmt.Sprintf("  %s · %s · %s", e.Type, e.Title, e.Ref)
				} else {
					line = fmt.Sprintf("  %s · %s · %s · %s", e.Type, e.Title, e.Summary, e.Ref)
				}
				sb.WriteString(line + "\n")
			}
		}

		sb.WriteString("\n--- End of knowledge index ---\n")
		fmt.Print(sb.String())
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
