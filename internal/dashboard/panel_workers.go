package dashboard

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/table"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/nats-io/nats.go"
)

// Colored status dots by babysitter state — mirrors the Pi status line
// (🟢 done · 🔴 failed · 🟡 waiting · 🔵 working).
const (
	IconAwaiting  = "🟡"
	IconActive    = "🔵"
	IconCompleted = "🟢"
	IconFailed    = "🔴"
)

// procEmoji maps a babysitter process name to an emoji (mirrors the babysitter
// extension). Empty for non-process (freeform) vendangeurs.
func procEmoji(p string) string {
	switch p {
	case "":
		return ""
	case "swe":
		return "🛠️"
	case "build":
		return "🏗️"
	case "release", "publish":
		return "🚀"
	}
	lp := strings.ToLower(p)
	switch {
	case strings.Contains(lp, "build"):
		return "🏗️"
	case strings.Contains(lp, "release"), strings.Contains(lp, "publish"), strings.Contains(lp, "deploy"):
		return "🚀"
	case strings.Contains(lp, "test"):
		return "🧪"
	default:
		return "⚙️"
	}
}

// WorkerEntry represents a single worker entry from the NATS KV bucket.
type WorkerEntry struct {
	Key     string
	Project string
	Tempo   string // active | awaiting | completed | failed
	Step    string // current step/status description
	State   string // running | stopped
	Process string
}

// StatusIcon returns the colored dot matching the worker's tempo/state.
func (w WorkerEntry) StatusIcon() string {
	switch w.Tempo {
	case "active":
		return IconActive
	case "completed":
		return IconCompleted
	case "failed":
		return IconFailed
	case "awaiting", "blocked":
		return IconAwaiting
	default:
		return IconAwaiting
	}
}

// StatusText returns process emoji + colored dot + step/tempo for the status column.
func (w WorkerEntry) StatusText() string {
	icon := w.StatusIcon()
	label := w.Step
	if label == "" {
		label = w.Tempo
		if label == "" {
			label = "awaiting events"
		}
	}
	if pe := procEmoji(w.Process); pe != "" {
		return fmt.Sprintf("%s %s %s", pe, icon, label)
	}
	return fmt.Sprintf("%s %s", icon, label)
}

// kvUpdateMsg is sent when the KV watcher observes a change.
type kvUpdateMsg struct {
	workers []WorkerEntry
}

// kvErrorMsg is sent when the KV watch encounters an error.
type kvErrorMsg struct{ err error }

// WorkersPanel is the Panel implementation for the Workers view.
type WorkersPanel struct {
	vignoble string
	kv       nats.KeyValue

	workers  []WorkerEntry // ordered for stable display
	filter   string        // current filter text (empty = show all)

	table     table.Model
	filterBox textinput.Model

	filtering bool // true when the filter input is open
	focused   bool
	width     int
	height    int

	lastErr error
	lastUpdate time.Time
}

// NewWorkersPanel creates a new workers panel. kv may be nil — the panel
// renders gracefully when NATS is unavailable.
func NewWorkersPanel(vignoble string, kv nats.KeyValue) *WorkersPanel {
	cols := []table.Column{
		{Title: "Worker", Width: 24},
		{Title: "Project", Width: 12},
		{Title: "Status", Width: 30},
	}

	t := table.New(
		table.WithColumns(cols),
		table.WithFocused(false),
		table.WithHeight(6),
	)
	t.SetStyles(tableStyles())

	fi := textinput.New()
	fi.Placeholder = "filter vendangeurs…"
	fi.CharLimit = 60

	return &WorkersPanel{
		vignoble:  vignoble,
		kv:        kv,
		table:     t,
		filterBox: fi,
	}
}

// tableStyles returns subtle table styling.
func tableStyles() table.Styles {
	s := table.DefaultStyles()
	s.Header = s.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		BorderBottom(true).
		Bold(false).
		Foreground(lipgloss.Color("240"))
	s.Selected = s.Selected.
		Foreground(lipgloss.Color("229")).
		Background(lipgloss.Color("57")).
		Bold(false)
	return s
}

// ── Panel interface ──────────────────────────────────────────────────────────

func (p *WorkersPanel) Title() string { return "🧺 Vendangeurs" }

func (p *WorkersPanel) Init() tea.Cmd {
	if p.kv != nil {
		return p.watchKV()
	}
	return nil
}

func (p *WorkersPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case kvUpdateMsg:
		p.workers = msg.workers
		p.lastUpdate = time.Now()
		p.lastErr = nil
		p.rebuildTable()

	case kvErrorMsg:
		p.lastErr = msg.err

	case tea.KeyMsg:
		if p.filtering {
			switch msg.String() {
			case "esc", "enter":
				p.filtering = false
				p.filterBox.Blur()
			default:
				var cmd tea.Cmd
				p.filterBox, cmd = p.filterBox.Update(msg)
				cmds = append(cmds, cmd)
				p.filter = p.filterBox.Value()
				p.rebuildTable()
			}
			return p, tea.Batch(cmds...)
		}

		if !p.focused {
			break
		}
		switch msg.String() {
		case "j", "down":
			p.table.MoveDown(1)
		case "k", "up":
			p.table.MoveUp(1)
		case "/":
			p.filtering = true
			p.filterBox.SetValue("")
			p.filter = ""
			p.filterBox.Focus()
			p.rebuildTable()
		case "esc":
			if p.filter != "" {
				p.filter = ""
				p.filterBox.SetValue("")
				p.rebuildTable()
			}
		}
	}

	// Forward key events to the table when focused and not filtering
	if !p.filtering {
		var cmd tea.Cmd
		p.table, cmd = p.table.Update(msg)
		cmds = append(cmds, cmd)
	}

	return p, tea.Batch(cmds...)
}

func (p *WorkersPanel) View() string {
	var sb strings.Builder

	if p.filtering {
		sb.WriteString(p.filterBox.View())
		sb.WriteString("\n")
	} else if p.filter != "" {
		filterStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
		sb.WriteString(filterStyle.Render(fmt.Sprintf("filter: %s", p.filter)))
		sb.WriteString("\n")
	}

	if p.lastErr != nil {
		errStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("196"))
		sb.WriteString(errStyle.Render(fmt.Sprintf("⚠ NATS error: %v", p.lastErr)))
		return sb.String()
	}

	rows := p.filteredRows()
	if len(rows) == 0 {
		dimStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
		if p.filter != "" {
			sb.WriteString(dimStyle.Render("(no vendangeurs match filter)"))
		} else {
			sb.WriteString(dimStyle.Render("(no active vendangeurs)"))
		}
		return sb.String()
	}

	sb.WriteString(p.table.View())
	return sb.String()
}

func (p *WorkersPanel) SetSize(width, height int) {
	p.width = width
	p.height = height

	// Adjust column widths to fill available space
	nameW := max(20, width-48)
	cols := []table.Column{
		{Title: "Worker", Width: nameW},
		{Title: "Project", Width: 12},
		{Title: "Status", Width: 30},
	}

	tableH := height - 3 // header row + filter row + border margin
	if p.filtering {
		tableH--
	}
	if tableH < 1 {
		tableH = 1
	}

	p.table.SetColumns(cols)
	p.table.SetHeight(tableH)
	p.filterBox.Width = width - 4
}

func (p *WorkersPanel) SetFocused(focused bool) {
	p.focused = focused
	t := p.table
	t.SetStyles(tableStyles()) // re-apply; focus border is handled by dashboard
	if focused {
		t.Focus()
	} else {
		t.Blur()
	}
	p.table = t
}

// ── KV watch ────────────────────────────────────────────────────────────────

// watchKV starts a goroutine that watches the pinard-agents KV bucket and
// sends kvUpdateMsg messages to the Bubble Tea runtime.
func (p *WorkersPanel) watchKV() tea.Cmd {
	return func() tea.Msg {
		watcher, err := p.kv.WatchAll()
		if err != nil {
			return kvErrorMsg{err: fmt.Errorf("KV watch: %w", err)}
		}
		defer watcher.Stop()

		workers := map[string]WorkerEntry{}

		for entry := range watcher.Updates() {
			if entry == nil {
				// nil signals end of initial values snapshot
				break
			}
			we, ok := parseKVEntry(entry)
			if !ok {
				continue
			}
			if entry.Operation() == nats.KeyValueDelete || entry.Operation() == nats.KeyValuePurge {
				delete(workers, entry.Key())
			} else {
				// Only keep workers for this vignoble
				if p.vignoble == "" || we.vignoble == p.vignoble {
					workers[entry.Key()] = we.WorkerEntry
				}
			}
		}

		snapshot := sortedWorkers(workers)
		// Return snapshot and re-subscribe for live updates
		return kvUpdateMsg{workers: snapshot}
	}
}

// watchKVLive returns a tea.Cmd that watches for live updates after the
// initial snapshot.  Call this from Update after receiving kvUpdateMsg.
func (p *WorkersPanel) watchKVLive() tea.Cmd {
	if p.kv == nil {
		return nil
	}
	return func() tea.Msg {
		watcher, err := p.kv.WatchAll()
		if err != nil {
			return kvErrorMsg{err: err}
		}
		defer watcher.Stop()

		workers := make(map[string]WorkerEntry)
		// Rebuild from current state
		for _, w := range p.workers {
			workers[w.Key] = w
		}

		for entry := range watcher.Updates() {
			if entry == nil {
				continue
			}
			if entry.Operation() == nats.KeyValueDelete || entry.Operation() == nats.KeyValuePurge {
				delete(workers, entry.Key())
			} else {
				we, ok := parseKVEntry(entry)
				if !ok {
					continue
				}
				if p.vignoble == "" || we.vignoble == p.vignoble {
					workers[entry.Key()] = we.WorkerEntry
				}
			}
			return kvUpdateMsg{workers: sortedWorkers(workers)}
		}
		return kvErrorMsg{err: fmt.Errorf("KV watcher closed")}
	}
}

// kvEntryWithVignoble is a helper struct used only during parsing.
type kvEntryWithVignoble struct {
	WorkerEntry
	vignoble string
}

func parseKVEntry(entry nats.KeyValueEntry) (kvEntryWithVignoble, bool) {
	if entry.Value() == nil {
		return kvEntryWithVignoble{}, false
	}
	var data map[string]any
	if err := json.Unmarshal(entry.Value(), &data); err != nil {
		return kvEntryWithVignoble{}, false
	}

	we := WorkerEntry{Key: entry.Key()}
	we.Project, _ = data["project"].(string)
	we.Tempo, _ = data["tempo"].(string)
	we.Step, _ = data["step"].(string)
	we.State, _ = data["state"].(string)
	we.Process, _ = data["process"].(string)

	// Derive step from process name if not set
	if we.Step == "" && we.Process != "" {
		we.Step = we.Process
	}

	vig, _ := data["vignoble"].(string)
	return kvEntryWithVignoble{WorkerEntry: we, vignoble: vig}, true
}

func sortedWorkers(m map[string]WorkerEntry) []WorkerEntry {
	out := make([]WorkerEntry, 0, len(m))
	for _, v := range m {
		out = append(out, v)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Key < out[j].Key
	})
	return out
}

// ── Table helpers ────────────────────────────────────────────────────────────

func (p *WorkersPanel) rebuildTable() {
	rows := p.filteredRows()
	p.table.SetRows(rows)
	// Reset cursor if out of bounds
	if len(rows) == 0 {
		p.table.SetCursor(0)
	}
}

func (p *WorkersPanel) filteredRows() []table.Row {
	rows := make([]table.Row, 0, len(p.workers))
	for _, w := range p.workers {
		if p.filter != "" && !strings.Contains(strings.ToLower(w.Key), strings.ToLower(p.filter)) {
			continue
		}
		rows = append(rows, table.Row{w.Key, w.Project, w.StatusText()})
	}
	return rows
}

// max returns the larger of two ints.
