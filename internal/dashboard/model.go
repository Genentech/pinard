package dashboard

import (
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/nats-io/nats.go"
)

// Layout:
//
//	┌─ Workers ──────────┬─ MRs ─────────────┐
//	│                    │                   │
//	├─ Pipelines ────────┼─ Events ──────────┤
//	│                    │                   │
//	└────────────────────┴───────────────────┘
//	┌─ Parcelles ───────────────────────────┐
//	└───────────────────────────────────────┘
type Model struct {
	panels   []Panel
	focused  int
	width    int
	height   int
	quitting bool
}

func New() Model {
	return newModel(NewMRsPanel(), nil, "", nil, "", false)
}

// NewWithStatePath creates a dashboard model that reads MR state from the
// given mr-watcher.yaml path.
func NewWithStatePath(mrStatePath string) Model {
	return newModel(NewMRsPanelWithPath(mrStatePath), nil, "", nil, "", false)
}

// NewWithOptions creates a dashboard model with explicit data sources.
func NewWithOptions(mrStatePath string, js nats.JetStreamContext, vignobleDir string, nc *nats.Conn, vignoble string, cloudConfigured bool) Model {
	return newModel(NewMRsPanelWithPath(mrStatePath), js, vignobleDir, nc, vignoble, cloudConfigured)
}

func newModel(mrsPanel Panel, js nats.JetStreamContext, vignobleDir string, nc *nats.Conn, vignoble string, cloudConfigured bool) Model {
	var workersPanel Panel
	if js != nil {
		kv, err := js.KeyValue("pinard-agents")
		if err == nil {
			// Fall back to extracting vignoble name from dir if not provided.
			v := vignoble
			if v == "" && vignobleDir != "" {
				parts := strings.Split(vignobleDir, "/")
				last := parts[len(parts)-1]
				v = strings.TrimPrefix(last, "vignoble-")
			}
			workersPanel = NewWorkersPanel(v, kv)
		}
	}
	if workersPanel == nil {
		workersPanel = NewPlaceholderPanel("🧺 Vendangeurs")
	}

	// Resolve vignoble name for the engram panel if not provided.
	v := vignoble
	if v == "" && vignobleDir != "" {
		parts := strings.Split(vignobleDir, "/")
		v = strings.TrimPrefix(parts[len(parts)-1], "vignoble-")
	}
	engramPanel := NewEngramPanel(vignobleDir, v, cloudConfigured, js)

	panels := []Panel{
		workersPanel,
		mrsPanel,
		NewParcellesPanelWithDir(vignobleDir, nc),
		NewEventsPanel(js),
		NewPipelinesPanelWithDir(vignobleDir),
		engramPanel,
	}
	panels[0].SetFocused(true)
	return Model{panels: panels}
}

func (m Model) Init() tea.Cmd {
	cmds := make([]tea.Cmd, len(m.panels))
	for i, p := range m.panels {
		cmds[i] = p.Init()
	}
	return tea.Batch(cmds...)
}

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.distributeSizes()
		return m, nil

	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			m.quitting = true
			return m, tea.Quit
		case "tab":
			m.panels[m.focused].SetFocused(false)
			m.focused = (m.focused + 1) % len(m.panels)
			m.panels[m.focused].SetFocused(true)
			return m, nil
		case "shift+tab":
			m.panels[m.focused].SetFocused(false)
			m.focused = (m.focused - 1 + len(m.panels)) % len(m.panels)
			m.panels[m.focused].SetFocused(true)
			return m, nil
		case "r":
			// Force refresh — re-send init
			cmds := make([]tea.Cmd, len(m.panels))
			for i, p := range m.panels {
				cmds[i] = p.Init()
			}
			return m, tea.Batch(cmds...)
		}
		// Forward j/k and other keys to focused panel
		updated, cmd := m.panels[m.focused].Update(msg)
		m.panels[m.focused] = updated
		return m, cmd
	}

	// Broadcast other messages to all panels
	var cmds []tea.Cmd
	for i, p := range m.panels {
		updated, cmd := p.Update(msg)
		m.panels[i] = updated
		if cmd != nil {
			cmds = append(cmds, cmd)
		}
	}
	return m, tea.Batch(cmds...)
}

func (m *Model) distributeSizes() {
	if m.width == 0 || m.height == 0 {
		return
	}
	// Help bar at bottom: 1 line
	contentH := m.height - 1

	halfW := m.width / 2
	rightW := m.width - halfW

	// Four rows: top, middle, pipelines, engram (small).
	row1H := contentH / 5         // ~20% for Workers | MRs
	row2H := contentH / 5         // ~20% for Parcelles | Events
	row4H := 6                    // fixed small row for Engram
	row3H := contentH - row1H - row2H - row4H // remainder for Pipelines
	if row3H < 4 {
		row3H = 4
	}

	// Top-left: Workers (0), Top-right: MRs (1)
	m.panels[0].SetSize(halfW, row1H)
	m.panels[1].SetSize(rightW, row1H)
	// Middle-left: Parcelles (2), Middle-right: Events (3)
	m.panels[2].SetSize(halfW, row2H)
	m.panels[3].SetSize(rightW, row2H)
	// Full-width: Pipelines (4)
	m.panels[4].SetSize(m.width, row3H)
	// Full-width: Engram (5)
	m.panels[5].SetSize(m.width, row4H)
}

func (m Model) View() string {
	if m.quitting {
		return ""
	}
	if m.width == 0 {
		return "Initializing..."
	}

	// Row 1: Workers | MRs
	row1 := lipgloss.JoinHorizontal(lipgloss.Top,
		m.panels[0].View(),
		m.panels[1].View(),
	)
	// Row 2: Parcelles | Events
	row2 := lipgloss.JoinHorizontal(lipgloss.Top,
		m.panels[2].View(),
		m.panels[3].View(),
	)
	// Row 3: Pipelines (full width)
	row3 := m.panels[4].View()
	// Row 4: Engram (full width)
	row4 := m.panels[5].View()

	helpText := "tab: focus  j/k: scroll  r: refresh  q: quit"
	if m.focused == 2 { // Parcelles panel
		helpText = "j/k: scroll  enter: select  a: archived  d: delete  tab: focus  q: quit"
	}
	help := helpStyle.Render(helpText)

	return strings.Join([]string{row1, row2, row3, row4, help}, "\n")
}
