## Purpose

A standalone TUI binary (`aoc dashboard`) that provides a live, always-visible overview of the pinard system. Runs in a tmux split pane alongside the conductor, powered by Bubble Tea (charmbracelet/bubbletea).

## Background

The current conductor widget (`setWidget`) renders text lines below the editor with no layout control — doesn't adapt to terminal width, not interactive, limited space. The full-screen TUI component (`DashboardComponent`) is a modal overlay that blocks conductor interaction.

What's needed: an independent process that connects to NATS, reads KV state and parcelle journals, and renders a continuously-updating dashboard in its own tmux pane.

## Architecture

```
tmux session "conductor"
┌─────────────────────────────────┬──────────────────────┐
│ Pi (conductor)                  │ aoc dashboard        │
│                                 │                      │
│ User interaction, LLM,          │ Workers panel        │
│ commands                        │ Pipeline view        │
│                                 │ MR status            │
│                                 │ Events feed          │
│                                 │ Parcelle overview    │
└─────────────────────────────────┴──────────────────────┘
```

`aoc dashboard` is a Go binary using:
- **Bubble Tea** — TUI framework (event loop, model-update-view)
- **Lip Gloss** — styling (colors, borders, padding)
- **Bubbles** — components (tables, spinners, viewports)
- NATS subscription — live KV watch + event stream
- Filesystem — reads parcelle journals for pipeline view

## Panels

### Workers

Live table of active workers from KV state:

```
┌─ Workers ─────────────────────────────────────────┐
│ exo-cli-swe-1       swe   ⏳ awaiting events      │
│ exo-cli-review-128  review ▸ review-code          │
│ charon-swe-310      swe   ▸ implement             │
└───────────────────────────────────────────────────┘
```

### Pipelines

Process step timeline from babysitter journals:

```
┌─ Pipelines ───────────────────────────────────────┐
│ exo-cli-swe-1                                     │
│   ✓plan → ✓impl → ✓test → ✓mr → ⏳review-loop   │
│                                                   │
│ exo-cli-review-128                                │
│   ✓fetch → ▸review                               │
└───────────────────────────────────────────────────┘
```

### MRs

Tracked MRs with pipeline/review status:

```
┌─ MRs ─────────────────────────────────────────────┐
│ !128  exo-cli   ✓ CI  ⏳ approval  auto-merge     │
│ !310  charon    ✗ CI  attempt 2/5                  │
└───────────────────────────────────────────────────┘
```

### Events

Live feed of recent events (from NATS stream):

```
┌─ Events ──────────────────────────────────────────┐
│ 02:15 pipeline_passed    exo-cli !128             │
│ 02:14 review_comment     exo-cli !128             │
│ 02:10 process_completed  exo-cli-review-128       │
│ 01:55 needs_approval     exo-cli !128             │
└───────────────────────────────────────────────────┘
```

### Parcelles

Overview of active workstreams:

```
┌─ Parcelles ───────────────────────────────────────┐
│ ● exo-cli       2 workers  3 runs  1 pending gate │
│ ● reviews       1 worker   2 runs                 │
│ ○ charon        0 workers  1 run                  │
└───────────────────────────────────────────────────┘
```

## Data Sources

| Panel | Source | Update mechanism |
|---|---|---|
| Workers | KV bucket `pinard-agents` | NATS KV watch (live) |
| Pipelines | `vignoble/parcelles/*/runs/*/journal/` | Filesystem poll (5s) |
| MRs | `.state/mr-watcher.yaml` | Filesystem poll (10s) |
| Events | NATS stream `pinard-agent-events` | JetStream consumer (live) |
| Parcelles | `vignoble/parcelles/*/` dirs + runs | Filesystem poll (10s) |

## Keyboard Controls

| Key | Action |
|---|---|
| `tab` | Cycle focus between panels |
| `j/k` | Scroll within focused panel |
| `q` | Quit dashboard |
| `r` | Force refresh all panels |
| `/` | Filter workers/events |
| `enter` | Expand selected item (show journal details) |

## Implementation

### Dependencies

```go
import (
    tea "github.com/charmbracelet/bubbletea"
    "github.com/charmbracelet/lipgloss"
    "github.com/charmbracelet/bubbles/table"
    "github.com/charmbracelet/bubbles/viewport"
)
```

### Command

```go
// cmd/aoc/cmd_dashboard.go
var dashboardCmd = &cobra.Command{
    Use:   "dashboard",
    Short: "Live TUI dashboard for pinard system",
    RunE: func(cmd *cobra.Command, args []string) error {
        // Connect NATS
        // Start KV watcher
        // Start event consumer
        // Run Bubble Tea program
    },
}
```

### Auto-launch from conductor

The conductor's `bin/pinard` launcher can auto-split a tmux pane:

```bash
# In conductor mode, after exec pi:
# Split right pane with dashboard
tmux split-window -h -t "$PINARD_SOCKET:conductor" "aoc dashboard --vignoble $VIGNOBLE"
```

Or the conductor extension spawns it on `session_start`.

## Open Questions

1. **Terminal width** — Should the dashboard adapt to narrow panes (stack panels vertically) or require a minimum width?

2. **Conductor integration** — Should the conductor extension remove the `setWidget` dashboard entirely and rely on `aoc dashboard`? Or keep both (widget = minimal, dashboard = full)?

3. **Remote dashboard** — Could `aoc dashboard` run on a different machine, connecting to the same NATS server? Useful for monitoring HPC pipelines from a laptop.

4. **Notification sound** — Should critical events (process_failed, circuit_breaker) trigger a terminal bell?
