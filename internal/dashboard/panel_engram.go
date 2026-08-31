package dashboard

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/nats-io/nats.go"

	"github.com/Genentech/pinard/internal/engram"
)

const engramPollInterval = 10 * time.Second

// engramRefreshMsg is sent when the panel finishes polling.
type engramRefreshMsg struct {
	entries []engramEntry
}

// engramTickMsg triggers the next poll.
type engramTickMsg struct{}

// engramEntry holds the display data for one vignoble's engram state.
type engramEntry struct {
	Vignoble       string
	Status         *engram.DBStatus
	SyncRecord     *engram.SyncRecord
	CloudConfigured bool
	Err            string
}

func (e engramEntry) verdict() string {
	warnStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
	if e.Err != "" {
		return errStyle.Render("⚠ " + e.Err)
	}
	if e.Status == nil || !e.Status.DBExists {
		return dimStyle.Render("· no store")
	}
	if !e.CloudConfigured {
		return dimStyle.Render("· local-only")
	}
	if e.SyncRecord != nil && e.SyncRecord.Result == "error" {
		return errStyle.Render("✗ sync error")
	}
	if e.Status.IsDegraded() {
		msg := fmt.Sprintf("⚠ degraded: %d blocked", e.Status.UnackedMutations)
		if reason := e.Status.ReasonPhrase(); reason != "" {
			msg += " (" + reason + ")"
		}
		return warnStyle.Render(msg)
	}
	if e.Status.UnackedMutations > 0 {
		return warnStyle.Render(fmt.Sprintf("⚠ %d pending push", e.Status.UnackedMutations))
	}
	return okStyle.Render("✓ synced")
}

func (e engramEntry) lastSyncStr() string {
	if e.SyncRecord == nil || e.SyncRecord.LastSync.IsZero() {
		return "never"
	}
	age := time.Since(e.SyncRecord.LastSync)
	switch {
	case age < time.Minute:
		return "just now"
	case age < time.Hour:
		return fmt.Sprintf("%dm ago", int(age.Minutes()))
	default:
		return e.SyncRecord.LastSync.Local().Format("01-02 15:04")
	}
}

var (
	okStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("2"))
	errStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("1"))
)

// EngramPanel displays the engram cloud sync status for the vignoble.
type EngramPanel struct {
	vignobleDir string
	vignoble    string
	js          nats.JetStreamContext
	cloudConfigured bool

	entries []engramEntry
	focused bool
	width   int
	height  int
	lastErr string
}

// NewEngramPanel creates a new EngramPanel. vignobleDir is the path to the
// vignoble directory; js may be nil (NATS unavailable).
func NewEngramPanel(vignobleDir, vignoble string, cloudConfigured bool, js nats.JetStreamContext) *EngramPanel {
	return &EngramPanel{
		vignobleDir:    vignobleDir,
		vignoble:       vignoble,
		js:             js,
		cloudConfigured: cloudConfigured,
		width:          40,
		height:         6,
	}
}

func (p *EngramPanel) Title() string          { return "🧠 Engram" }
func (p *EngramPanel) SetFocused(f bool)      { p.focused = f }
func (p *EngramPanel) SetSize(w, h int)       { p.width = w; p.height = h }

func (p *EngramPanel) Init() tea.Cmd {
	return tea.Batch(p.pollCmd(), p.tickCmd())
}

func (p *EngramPanel) tickCmd() tea.Cmd {
	return tea.Tick(engramPollInterval, func(time.Time) tea.Msg {
		return engramTickMsg{}
	})
}

func (p *EngramPanel) pollCmd() tea.Cmd {
	vignobleDir := p.vignobleDir
	vignoble := p.vignoble
	js := p.js
	cloudConfigured := p.cloudConfigured

	return func() tea.Msg {
		entries := pollEngramStatus(vignobleDir, vignoble, cloudConfigured, js)
		return engramRefreshMsg{entries: entries}
	}
}

func (p *EngramPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	switch msg.(type) {
	case engramRefreshMsg:
		p.entries = msg.(engramRefreshMsg).entries
		return p, nil
	case engramTickMsg:
		return p, tea.Batch(p.pollCmd(), p.tickCmd())
	}
	return p, nil
}

func (p *EngramPanel) View() string {
	content := p.renderContent()
	return renderPanel(p.Title(), content, p.focused, p.width, p.height)
}

func (p *EngramPanel) renderContent() string {
	if len(p.entries) == 0 {
		return dimStyle.Render("loading…")
	}

	var sb strings.Builder
	for _, e := range p.entries {
		if e.Status == nil || !e.Status.DBExists {
			sb.WriteString(fmt.Sprintf("%-14s %s\n", truncate(e.Vignoble, 14), e.verdict()))
			continue
		}
		lastSync := ""
		if e.CloudConfigured {
			lastSync = fmt.Sprintf("  sync:%s", e.lastSyncStr())
		}
		sb.WriteString(fmt.Sprintf("%-14s total:%-4d unacked:%-4d%s %s\n",
			truncate(e.Vignoble, 14),
			e.Status.Total,
			e.Status.UnackedMutations,
			lastSync,
			e.verdict(),
		))
	}
	return strings.TrimRight(sb.String(), "\n")
}

// pollEngramStatus queries the local db and NATS KV for one vignoble.
func pollEngramStatus(vignobleDir, vignoble string, cloudConfigured bool, js nats.JetStreamContext) []engramEntry {
	if vignobleDir == "" || vignoble == "" {
		return nil
	}

	dbPath := filepath.Join(vignobleDir, ".engram", "engram.db")
	st, err := engram.QueryStatus(dbPath)
	entry := engramEntry{
		Vignoble:       vignoble,
		CloudConfigured: cloudConfigured,
	}
	if err != nil {
		entry.Err = err.Error()
		return []engramEntry{entry}
	}
	entry.Status = st

	// Fetch last-sync record from NATS KV.
	if js != nil && cloudConfigured {
		if kv, err := js.KeyValue("pinard-engram"); err == nil {
			if kvEntry, err := kv.Get(vignoble); err == nil && kvEntry.Value() != nil {
				var data map[string]any
				if json.Unmarshal(kvEntry.Value(), &data) == nil {
					if b, e := json.Marshal(data); e == nil {
						var rec engram.SyncRecord
						if json.Unmarshal(b, &rec) == nil {
							entry.SyncRecord = &rec
						}
					}
				}
			}
		}
	}

	return []engramEntry{entry}
}
