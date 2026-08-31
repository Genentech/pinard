// Package dashboard implements the `aoc dashboard` TUI panels.
// Each panel implements the Panel interface and is rendered as a bordered box
// in the Bubble Tea model.
package dashboard

import (
	tea "github.com/charmbracelet/bubbletea"
)

// Panel is the interface all dashboard panels must implement.
type Panel interface {
	// Title returns the panel's header label.
	Title() string

	// Init returns the initial command for the panel (if any).
	Init() tea.Cmd

	// Update handles incoming messages. Returns the updated panel and any commands.
	Update(msg tea.Msg) (Panel, tea.Cmd)

	// View renders the panel content (without the border — the dashboard
	// wraps each panel's View() output in a lipgloss border).
	View() string

	// SetSize informs the panel of its available width and height.
	SetSize(width, height int)

	// SetFocused tells the panel whether it currently holds keyboard focus.
	SetFocused(focused bool)
}
