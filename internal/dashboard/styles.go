package dashboard

import "github.com/charmbracelet/lipgloss"

var (
	panelBorder = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("240")).
			Padding(0, 1)

	panelBorderFocused = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(lipgloss.Color("62")).
				Padding(0, 1)

	titleStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("62")).
			Bold(true)

	dimStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("240"))

	helpStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("240"))
)

func renderPanel(title, content string, focused bool, width, height int) string {
	style := panelBorder
	if focused {
		style = panelBorderFocused
	}
	// Account for border (2) + title (1) vertically, border (2) + padding (2) horizontally
	innerW := width - 4
	innerH := height - 3
	if innerW < 1 {
		innerW = 1
	}
	if innerH < 1 {
		innerH = 1
	}

	body := lipgloss.NewStyle().
		Width(innerW).
		Height(innerH).
		MaxHeight(innerH).
		Render(content)

	return style.Width(width - 2).Render(lipgloss.JoinVertical(lipgloss.Left,
		titleStyle.Render(title),
		body,
	))
}
