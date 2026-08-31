package dashboard

import tea "github.com/charmbracelet/bubbletea"

// PlaceholderPanel is an empty panel used during scaffolding.
// Each real panel (Workers, MRs, Events, Pipelines) will replace it.
type PlaceholderPanel struct {
	title   string
	focused bool
	width   int
	height  int
}

// NewPlaceholderPanel creates a named empty panel.
func NewPlaceholderPanel(title string) *PlaceholderPanel {
	return &PlaceholderPanel{title: title, width: 40, height: 10}
}

func (p *PlaceholderPanel) Init() tea.Cmd                        { return nil }
func (p *PlaceholderPanel) Update(msg tea.Msg) (Panel, tea.Cmd)  { return p, nil }
func (p *PlaceholderPanel) View() string {
	return renderPanel(p.title, dimStyle.Render("(no data)"), p.focused, p.width, p.height)
}
func (p *PlaceholderPanel) Title() string                        { return p.title }
func (p *PlaceholderPanel) SetFocused(focused bool)   { p.focused = focused }
func (p *PlaceholderPanel) SetSize(width, height int) { p.width = width; p.height = height }
