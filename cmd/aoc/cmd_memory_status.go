package main

import (
	"encoding/json"
	"fmt"
	"os"
	"text/tabwriter"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

// memoryStatusResponse mirrors the shape served by the memory-ingester status handler.
type memoryStatusResponse struct {
	Engram    memStatusEngram    `json:"engram"`
	SurrealDB memStatusSurrealDB `json:"surrealdb"`
	Wiki      memStatusWiki      `json:"wiki"`
}

type memStatusEngram struct {
	Reachable bool  `json:"reachable"`
	Pending   int64 `json:"pending"`
}

type memStatusSurrealDB struct {
	TotalLag    int64                `json:"total_lag"`
	TotalFailed int64                `json:"total_failed"`
	Groups      []memStatusSurGroup  `json:"groups"`
}

type memStatusSurGroup struct {
	Group      string `json:"group"`
	EngramMax  int64  `json:"engram_max"`
	Cursor     int64  `json:"cursor"`
	Lag        int64  `json:"lag"`
	Entities   int64  `json:"entities"`
	Failed     int64  `json:"failed"`
	LastIngest string `json:"last_ingest"`
}

type memStatusWiki struct {
	TotalDocs   int64               `json:"total_docs"`
	NeedsReview int64               `json:"needs_review"`
	LastRollup  string              `json:"last_rollup"`
	Groups      []memStatusWikiGroup `json:"groups"`
}

type memStatusWikiGroup struct {
	Group       string `json:"group"`
	Docs        int64  `json:"docs"`
	AutoServe   int64  `json:"auto_serve"`
	NeedsReview int64  `json:"needs_review"`
}

var memoryStatusCmd = &cobra.Command{
	Use:   "memory-status",
	Short: "Show unified memory health (Engram replication + SurrealDB ingestion + wiki curation)",
	Long: `Sends a request to the memory-ingester's NATS status handler and prints
three sections — engram, surrealdb, wiki — as tables, followed by a one-line verdict.

Exits non-zero when any group has lag > 0, failed > 0, or the ingester is unreachable.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		vignoble, _ := cmd.Flags().GetString("vignoble")
		if vignoble == "" {
			vignoble = os.Getenv("NATS_VIGNOBLE")
		}
		if vignoble == "" {
			if vb, err := config.ResolveVignoble(); err == nil {
				vignoble = vb.Name
			}
		}
		if vignoble == "" {
			return fmt.Errorf("--vignoble or NATS_VIGNOBLE is required (or run from inside a vignoble directory)")
		}
		timeoutMs, _ := cmd.Flags().GetInt("timeout")
		asJSON, _ := cmd.Flags().GetBool("json")

		creds, err := config.LoadCredentials()
		if err != nil {
			return fmt.Errorf("load credentials: %w", err)
		}

		nc := pnats.NewClient(creds)
		defer nc.Close()

		subject := pnats.MemoryStatusSubject(vignoble)
		timeout := time.Duration(timeoutMs) * time.Millisecond

		msg, err := nc.Request(subject, map[string]any{}, timeout)
		if err != nil {
			fmt.Fprintf(os.Stderr, "memory-status: no response from ingester (%v)\n", err)
			os.Exit(1)
		}

		var resp memoryStatusResponse
		if err := json.Unmarshal(msg.Data, &resp); err != nil {
			return fmt.Errorf("parse response: %w", err)
		}

		if asJSON {
			fmt.Println(string(msg.Data))
			return verdictExit(resp)
		}

		printMemoryStatus(resp)
		return verdictExit(resp)
	},
}

func printMemoryStatus(resp memoryStatusResponse) {
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)

	// Engram section
	fmt.Fprintln(w, "\n=== Engram (local→cloud replication) ===")
	fmt.Fprintf(w, "REACHABLE\tPENDING\n")
	fmt.Fprintf(w, "%v\t%d\n", resp.Engram.Reachable, resp.Engram.Pending)
	w.Flush()

	// SurrealDB section
	fmt.Fprintln(w, "\n=== SurrealDB (cloud→ingest) ===")
	fmt.Fprintf(w, "GROUP\tENGRAM_MAX\tCURSOR\tLAG\tENTITIES\tFAILED\tLAST_INGEST\n")
	for _, g := range resp.SurrealDB.Groups {
		last := fmtAge(g.LastIngest)
		fmt.Fprintf(w, "%s\t%d\t%d\t%d\t%d\t%d\t%s\n",
			g.Group, g.EngramMax, g.Cursor, g.Lag, g.Entities, g.Failed, last)
	}
	w.Flush()

	// Wiki section
	fmt.Fprintln(w, "\n=== Wiki curation ===")
	fmt.Fprintf(w, "GROUP\tDOCS\tAUTO_SERVE\tNEEDS_REVIEW\n")
	for _, g := range resp.Wiki.Groups {
		fmt.Fprintf(w, "%s\t%d\t%d\t%d\n", g.Group, g.Docs, g.AutoServe, g.NeedsReview)
	}
	w.Flush()

	// Verdict line
	fmt.Println()
	fmt.Println(verdictLine(resp))
}

func fmtAge(ts string) string {
	if ts == "" {
		return "—"
	}
	t, err := time.Parse(time.RFC3339, ts)
	if err != nil {
		return ts
	}
	age := time.Since(t)
	switch {
	case age < time.Minute:
		return "just now"
	case age < time.Hour:
		return fmt.Sprintf("%dm ago", int(age.Minutes()))
	default:
		return t.Local().Format("01-02 15:04")
	}
}

func verdictLine(resp memoryStatusResponse) string {
	// Health = ingest pipelines working. needs_review is a human backlog, not a
	// health signal, so it never drives the verdict (matches the pi-extension token).
	if resp.SurrealDB.TotalLag == 0 && resp.SurrealDB.TotalFailed == 0 {
		return "memory: ✓ ok"
	}
	msg := "memory: ⚠"
	sep := " "
	if resp.Engram.Pending > 0 {
		msg += sep + fmt.Sprintf("⏳%d", resp.Engram.Pending)
		sep = " · "
	}
	if resp.SurrealDB.TotalLag > 0 {
		msg += sep + fmt.Sprintf("ingest lag %d", resp.SurrealDB.TotalLag)
		sep = " · "
	}
	if resp.SurrealDB.TotalFailed > 0 {
		msg += sep + fmt.Sprintf("%d failed writes", resp.SurrealDB.TotalFailed)
	}
	return msg
}

func verdictExit(resp memoryStatusResponse) error {
	if resp.SurrealDB.TotalLag > 0 || resp.SurrealDB.TotalFailed > 0 {
		os.Exit(1)
	}
	return nil
}

func init() {
	memoryStatusCmd.Flags().String("vignoble", "", "Vignoble name (NATS namespace); defaults to NATS_VIGNOBLE env or cwd vignes.yaml")
	memoryStatusCmd.Flags().Int("timeout", 10000, "Request timeout in milliseconds")
	memoryStatusCmd.Flags().Bool("json", false, "Output raw JSON")
	rootCmd.AddCommand(memoryStatusCmd)
}
