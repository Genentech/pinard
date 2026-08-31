package dashboard

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/Genentech/pinard/internal/state"
)

const mrsPollInterval = 10 * time.Second

// mrRow is a single formatted row for one tracked MR.
type mrRow struct {
	iid     int
	project string
	line    string // pre-rendered display line
}

// MRsPanel shows tracked MRs from .state/mr-watcher.yaml.
type MRsPanel struct {
	statePath string // path to mr-watcher.yaml

	rows    []mrRow
	cursor  int
	focused bool
	width   int
	height  int

	err string
}

// mrsLoadedMsg is sent when the state file has been (re)loaded.
type mrsLoadedMsg struct {
	rows []mrRow
	err  string
}

// mrsTickMsg triggers a periodic reload.
type mrsTickMsg struct{}

// NewMRsPanel creates a new MRs panel. It reads the state file path from the
// PINARD_VIGNOBLE_STATE environment variable if set, otherwise falls back to
// ".state/mr-watcher.yaml" in the working directory.
func NewMRsPanel() Panel {
	statePath := os.Getenv("PINARD_VIGNOBLE_STATE")
	if statePath == "" {
		statePath = filepath.Join(".state", "mr-watcher.yaml")
	} else {
		statePath = filepath.Join(statePath, "mr-watcher.yaml")
	}
	return &MRsPanel{
		statePath: statePath,
		width:     40,
		height:    10,
	}
}

// NewMRsPanelWithPath creates an MRs panel that reads state from the given file path.
// Useful for the dashboard command (which knows the vignoble state dir) and for tests.
func NewMRsPanelWithPath(statePath string) *MRsPanel {
	return &MRsPanel{
		statePath: statePath,
		width:     40,
		height:    10,
	}
}

func (p *MRsPanel) Title() string           { return "MRs" }
func (p *MRsPanel) SetFocused(focused bool) { p.focused = focused }
func (p *MRsPanel) SetSize(w, h int) {
	p.width = w
	p.height = h
}

func (p *MRsPanel) Init() tea.Cmd {
	return tea.Batch(p.load(), p.tick())
}

func (p *MRsPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	switch msg := msg.(type) {
	case mrsLoadedMsg:
		if msg.err != "" {
			p.err = msg.err
		} else {
			p.err = ""
			p.rows = msg.rows
			// Clamp cursor
			if p.cursor >= len(p.rows) {
				p.cursor = max(0, len(p.rows)-1)
			}
		}
		return p, nil

	case mrsTickMsg:
		return p, tea.Batch(p.load(), p.tick())

	case tea.KeyMsg:
		if !p.focused {
			return p, nil
		}
		switch msg.String() {
		case "j", "down":
			if p.cursor < len(p.rows)-1 {
				p.cursor++
			}
		case "k", "up":
			if p.cursor > 0 {
				p.cursor--
			}
		}
		return p, nil
	}

	return p, nil
}

func (p *MRsPanel) View() string {
	var content string
	if p.err != "" {
		content = dimStyle.Render("error: " + p.err)
	} else if len(p.rows) == 0 {
		content = dimStyle.Render("(no tracked MRs)")
	} else {
		content = p.renderRows()
	}
	return renderPanel("MRs", content, p.focused, p.width, p.height)
}

// renderRows builds the visible portion of the rows list.
func (p *MRsPanel) renderRows() string {
	innerH := p.height - 4 // border (2) + title line (1) + padding (1)
	if innerH < 1 {
		innerH = 1
	}

	// Determine visible window
	start := 0
	if p.cursor >= innerH {
		start = p.cursor - innerH + 1
	}
	end := start + innerH
	if end > len(p.rows) {
		end = len(p.rows)
	}

	innerW := p.width - 4
	if innerW < 10 {
		innerW = 10
	}

	var lines []string
	for i := start; i < end; i++ {
		row := p.rows[i]
		line := row.line

		// Truncate to inner width
		if len(line) > innerW {
			line = line[:innerW]
		}

		if i == p.cursor && p.focused {
			line = lipgloss.NewStyle().
				Foreground(lipgloss.Color("62")).
				Bold(true).
				Render(line)
		}
		lines = append(lines, line)
	}

	// Scroll indicator when list is longer than visible area
	if len(p.rows) > innerH {
		indicator := fmt.Sprintf(" %d/%d", p.cursor+1, len(p.rows))
		lines = append(lines, dimStyle.Render(indicator))
	}

	return strings.Join(lines, "\n")
}

// load returns a tea.Cmd that reads mr-watcher.yaml and sends mrsLoadedMsg.
func (p *MRsPanel) load() tea.Cmd {
	path := p.statePath
	return func() tea.Msg {
		store, err := state.Load[state.MRWatcherState](path)
		if err != nil && !os.IsNotExist(err) {
			return mrsLoadedMsg{err: err.Error()}
		}
		rows := buildMRRows(store.Data.Watched)
		return mrsLoadedMsg{rows: rows}
	}
}

// tick schedules the next poll after mrsPollInterval.
func (p *MRsPanel) tick() tea.Cmd {
	return tea.Tick(mrsPollInterval, func(time.Time) tea.Msg {
		return mrsTickMsg{}
	})
}

// buildMRRows converts WatchedMR entries into display rows, sorted by IID.
// Exported for unit testing.
func buildMRRows(watched map[string]*state.WatchedMR) []mrRow {
	if len(watched) == 0 {
		return nil
	}

	rows := make([]mrRow, 0, len(watched))
	for _, mr := range watched {
		if mr.MR == 0 {
			continue
		}
		rows = append(rows, mrRow{
			iid:     mr.MR,
			project: mr.Project,
			line:    FormatMRLine(mr),
		})
	}

	sort.Slice(rows, func(i, j int) bool {
		return rows[i].iid < rows[j].iid
	})
	return rows
}

// FormatMRLine formats a WatchedMR into a single display string.
// Exported for unit testing.
//
// Example output:
//
//	!128  exo-cli   ✓ CI  ⏳ approval  auto-merge
//	!310  charon    ✗ CI  attempt 2/5
func FormatMRLine(mr *state.WatchedMR) string {
	var parts []string

	// MR IID — left-padded to 4 chars
	parts = append(parts, fmt.Sprintf("!%-4d", mr.MR))

	// Project — padded to 10 chars
	proj := mr.Project
	if len(proj) > 10 {
		proj = proj[:10]
	}
	parts = append(parts, fmt.Sprintf("%-10s", proj))

	// CI status
	if mr.PipelineFailCount > 0 {
		const maxRetries = 5
		parts = append(parts, fmt.Sprintf("✗ CI  attempt %d/%d", mr.PipelineFailCount, maxRetries))
	} else if mr.LastPipelineID > 0 {
		parts = append(parts, "✓ CI")
	}

	// Approval status
	if mr.NeedsApprovalNotified {
		parts = append(parts, "⏳ approval")
	}

	// Auto-merge label
	if mr.AutoMergeLabeled {
		parts = append(parts, "auto-merge")
	}

	// State overrides (post-merge, merged, etc.)
	switch mr.State {
	case "post_merge":
		parts = append(parts, fmt.Sprintf("post-merge check %d/10", mr.PostMergeChecks))
	case "merged":
		parts = append(parts, "✓ merged")
	}

	return strings.Join(parts, "  ")
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
