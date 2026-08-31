package dashboard

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// StepStatus represents the execution status of a single process step.
type StepStatus int

const (
	StepPending StepStatus = iota
	StepActive
	StepWaiting // waiting for external event
	StepDone
	StepFailed
)

// Step holds a single process step's name and status.
type Step struct {
	Label  string
	Status StepStatus
}

// WorkerPipeline holds a worker's name and its ordered list of steps.
type WorkerPipeline struct {
	WorkerName string // run ID (e.g. "exo-cli-swe-1")
	Parcelle   string
	Steps      []Step
	Completed  bool // RUN_COMPLETED seen
	Failed     bool // RUN_FAILED seen
}

// journalEntry is a minimal struct for deserializing journal JSON files.
type journalEntry struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data"`
}

// journalEffectData holds fields we care about from EFFECT_REQUESTED data.
type journalEffectData struct {
	EffectID string `json:"effectId"`
	TaskID   string `json:"taskId"`
	Label    string `json:"label"`
	Title    string `json:"title"`
	Kind     string `json:"kind"`
}

// pipelinesRefreshMsg is sent when poll completes.
type pipelinesRefreshMsg struct {
	pipelines []WorkerPipeline
}

// pipelinesTickMsg triggers the next poll.
type pipelinesTickMsg struct{}

// PipelinesPanel displays a timeline of babysitter process steps per worker.
type PipelinesPanel struct {
	vignoble string // path to vignoble directory
	focused  bool
	width    int
	height   int

	pipelines []WorkerPipeline
	cursor    int  // selected worker index (for j/k scrolling)
	scrollOff int  // line offset for viewport scrolling
	expanded  bool // enter expands selected worker to show step details

	lastErr error
}

// NewPipelinesPanel creates a PipelinesPanel rooted at vignoble.
func NewPipelinesPanel() Panel {
	vignoble := os.Getenv("PINARD_VIGNOBLE")
	return &PipelinesPanel{
		vignoble: vignoble,
		width:    40,
		height:   10,
	}
}

// NewPipelinesPanelWithDir creates a PipelinesPanel rooted at the given directory.
func NewPipelinesPanelWithDir(vignobleDir string) Panel {
	if vignobleDir == "" {
		vignobleDir = os.Getenv("PINARD_VIGNOBLE")
	}
	return &PipelinesPanel{
		vignoble: vignobleDir,
		width:    40,
		height:   10,
	}
}

func (p *PipelinesPanel) Title() string { return "Pipelines" }
func (p *PipelinesPanel) SetFocused(focused bool) { p.focused = focused }
func (p *PipelinesPanel) SetSize(w, h int) {
	p.width = w
	p.height = h
}

func (p *PipelinesPanel) Init() tea.Cmd {
	return tea.Batch(
		p.pollCmd(),
		p.tickCmd(),
	)
}

// tickCmd schedules the next poll after 5 seconds.
func (p *PipelinesPanel) tickCmd() tea.Cmd {
	return tea.Tick(5*time.Second, func(t time.Time) tea.Msg {
		return pipelinesTickMsg{}
	})
}

// pollCmd reads the journal directories and returns a pipelinesRefreshMsg.
func (p *PipelinesPanel) pollCmd() tea.Cmd {
	vignoble := p.vignoble
	return func() tea.Msg {
		pipelines := scanJournals(vignoble)
		return pipelinesRefreshMsg{pipelines: pipelines}
	}
}

func (p *PipelinesPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	switch msg := msg.(type) {
	case pipelinesRefreshMsg:
		p.pipelines = msg.pipelines
		// Clamp cursor
		if p.cursor >= len(p.pipelines) {
			p.cursor = max(0, len(p.pipelines)-1)
		}
		return p, nil

	case pipelinesTickMsg:
		return p, tea.Batch(p.pollCmd(), p.tickCmd())

	case tea.KeyMsg:
		if !p.focused {
			return p, nil
		}
		switch msg.String() {
		case "j", "down":
			if p.cursor < len(p.pipelines)-1 {
				p.cursor++
				p.expanded = false
			}
		case "k", "up":
			if p.cursor > 0 {
				p.cursor--
				p.expanded = false
			}
		case "enter":
			p.expanded = !p.expanded
		}
		return p, nil
	}
	return p, nil
}

func (p *PipelinesPanel) View() string {
	var content string
	if p.vignoble == "" {
		content = dimStyle.Render("PINARD_VIGNOBLE not set")
	} else if len(p.pipelines) == 0 {
		content = dimStyle.Render("no active runs")
	} else {
		content = p.renderPipelines()
	}
	return renderPanel(p.Title(), content, p.focused, p.width, p.height)
}

// ── Rendering ────────────────────────────────────────────────────────────────

var (
	stepDoneStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("2"))   // green
	stepActiveStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("3")).Bold(true) // yellow bold
	stepWaitStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("6"))   // cyan
	stepFailStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("1"))   // red
	workerNameStyle  = lipgloss.NewStyle().Bold(true)
	selectedRowStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("62")).Bold(true)
	arrowStyle       = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
)

func (p *PipelinesPanel) renderPipelines() string {
	innerW := p.width - 4 // account for border + padding
	if innerW < 10 {
		innerW = 10
	}
	// Visible height for content (border 2 + title 1)
	visibleH := p.height - 3
	if visibleH < 1 {
		visibleH = 1
	}

	// Build all lines and track which line the cursor starts on
	var lines []string
	cursorLine := 0
	for i, wp := range p.pipelines {
		if i == p.cursor {
			cursorLine = len(lines)
		}
		selected := i == p.cursor && p.focused

		// Worker name line
		nameStr := wp.WorkerName
		if wp.Parcelle != "" && wp.Parcelle != wp.WorkerName {
			nameStr = fmt.Sprintf("%s/%s", wp.Parcelle, wp.WorkerName)
		}
		var nameLine string
		if selected {
			nameLine = selectedRowStyle.Render("> " + truncate(nameStr, innerW-2))
		} else {
			nameLine = workerNameStyle.Render(truncate(nameStr, innerW))
		}
		lines = append(lines, nameLine)

		// Timeline line (always shown)
		timelineLine := "  " + renderTimeline(wp.Steps, innerW-2)
		lines = append(lines, timelineLine)

		// Expanded detail: show all steps with labels
		if selected && p.expanded && len(wp.Steps) > 0 {
			for _, s := range wp.Steps {
				icon := stepIcon(s.Status)
				detail := fmt.Sprintf("    %s %s", icon, s.Label)
				lines = append(lines, truncate(detail, innerW))
			}
		}

		// Blank separator between workers (except last)
		if i < len(p.pipelines)-1 {
			lines = append(lines, "")
		}
	}

	// Adjust scroll offset to keep cursor visible
	if cursorLine < p.scrollOff {
		p.scrollOff = cursorLine
	}
	if cursorLine >= p.scrollOff+visibleH {
		p.scrollOff = cursorLine - visibleH + 2 // +2 to show name + timeline
	}
	if p.scrollOff < 0 {
		p.scrollOff = 0
	}

	// Slice to visible window
	start := p.scrollOff
	end := start + visibleH
	if start > len(lines) {
		start = len(lines)
	}
	if end > len(lines) {
		end = len(lines)
	}

	return strings.Join(lines[start:end], "\n")
}

// renderTimeline renders steps as: ✓plan → ✓impl → ⏳review-loop
// Truncates from the left if too wide, showing a "…N→" prefix.
func renderTimeline(steps []Step, maxWidth int) string {
	if len(steps) == 0 {
		return dimStyle.Render("(no steps)")
	}

	// Build individual step strings
	parts := make([]string, len(steps))
	for i, s := range steps {
		icon := stepIcon(s.Status)
		var label string
		switch s.Status {
		case StepDone:
			label = stepDoneStyle.Render(icon) + dimStyle.Render(s.Label)
		case StepActive:
			label = stepActiveStyle.Render("▸ " + s.Label)
		case StepWaiting:
			label = stepWaitStyle.Render("⏳" + s.Label)
		case StepFailed:
			label = stepFailStyle.Render("✗" + s.Label)
		default:
			label = dimStyle.Render(icon + s.Label)
		}
		parts[i] = label
	}

	arrow := arrowStyle.Render(" → ")

	// Try to fit all; if not, show last N steps with a "…K→" prefix.
	full := strings.Join(parts, arrow)
	if visibleWidth(full) <= maxWidth {
		return full
	}

	// Find how many from the end fit
	for start := 1; start < len(parts); start++ {
		skipped := start
		prefix := dimStyle.Render(fmt.Sprintf("…%d", skipped)) + arrow
		subset := strings.Join(parts[start:], arrow)
		if visibleWidth(prefix)+visibleWidth(subset) <= maxWidth {
			return prefix + subset
		}
	}

	// Fallback: just the last step
	return parts[len(parts)-1]
}

func stepIcon(s StepStatus) string {
	switch s {
	case StepDone:
		return "✓"
	case StepActive:
		return "▸"
	case StepWaiting:
		return "⏳"
	case StepFailed:
		return "✗"
	default:
		return "○"
	}
}

// visibleWidth estimates the display width of a string (strips ANSI escapes).
// We do a rough approximation: count non-escape runes.
func visibleWidth(s string) int {
	inEscape := false
	w := 0
	for _, r := range s {
		if r == '\x1b' {
			inEscape = true
			continue
		}
		if inEscape {
			if r == 'm' {
				inEscape = false
			}
			continue
		}
		w++
	}
	return w
}

func truncate(s string, max int) string {
	runes := []rune(s)
	if len(runes) <= max {
		return s
	}
	if max <= 1 {
		return "…"
	}
	return string(runes[:max-1]) + "…"
}


// ── Journal scanning ─────────────────────────────────────────────────────────

// scanJournals walks vignoble/parcelles/*/runs/*/journal/ and returns
// one WorkerPipeline per run that has at least one step.
func scanJournals(vignoble string) []WorkerPipeline {
	if vignoble == "" {
		return nil
	}
	parcellesDir := filepath.Join(vignoble, "parcelles")
	parcelles, err := os.ReadDir(parcellesDir)
	if err != nil {
		return nil
	}

	var pipelines []WorkerPipeline
	// dedup by runID across parcelles
	seen := make(map[string]bool)

	for _, parcEntry := range parcelles {
		if !parcEntry.IsDir() {
			continue
		}
		parcelle := parcEntry.Name()
		runsDir := filepath.Join(parcellesDir, parcelle, "runs")
		runs, err := os.ReadDir(runsDir)
		if err != nil {
			continue
		}

		for _, runEntry := range runs {
			if !runEntry.IsDir() {
				continue
			}
			runID := runEntry.Name()
			if seen[runID] {
				continue
			}
			seen[runID] = true

			runDir := filepath.Join(runsDir, runID)
			wp, ok := parseRunJournal(runDir, runID, parcelle)
			if !ok {
				continue
			}
			pipelines = append(pipelines, wp)
		}
	}

	// Sort: active/waiting first, then by name
	sort.Slice(pipelines, func(i, j int) bool {
		ai := isActive(pipelines[i])
		aj := isActive(pipelines[j])
		if ai != aj {
			return ai // active first
		}
		return pipelines[i].WorkerName < pipelines[j].WorkerName
	})

	return pipelines
}

func isActive(wp WorkerPipeline) bool {
	return !wp.Completed && !wp.Failed
}

// parseRunJournal reads journal files for a run and returns a WorkerPipeline.
// Returns (wp, false) if the journal is empty or unreadable.
func parseRunJournal(runDir, runID, parcelle string) (WorkerPipeline, bool) {
	journalDir := filepath.Join(runDir, "journal")
	entries, err := os.ReadDir(journalDir)
	if err != nil || len(entries) == 0 {
		return WorkerPipeline{}, false
	}

	// Sort journal entries by name (they are seq-prefixed: 000001.ulid.json)
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Name() < entries[j].Name()
	})

	type inFlightStep struct {
		effectID string
		idx      int // index into steps
	}

	var steps []Step
	// map effectID → steps index for resolving
	inFlight := make(map[string]int)
	var completed, failed bool

	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(journalDir, e.Name()))
		if err != nil {
			continue
		}
		var entry journalEntry
		if err := json.Unmarshal(data, &entry); err != nil {
			continue
		}

		switch entry.Type {
		case "EFFECT_REQUESTED":
			var d journalEffectData
			if err := json.Unmarshal(entry.Data, &d); err == nil {
				label := pickLabel(d)
				status := StepActive
				if d.Kind == "event" {
					status = StepWaiting
				}
				idx := len(steps)
				steps = append(steps, Step{Label: label, Status: status})
				if d.EffectID != "" {
					inFlight[d.EffectID] = idx
				}
			}

		case "EFFECT_RESOLVED":
			// Resolve the matching in-flight step
			var d struct {
				EffectID string `json:"effectId"`
				Status   string `json:"status"` // "ok" or "error"
			}
			if err := json.Unmarshal(entry.Data, &d); err == nil {
				if idx, ok := inFlight[d.EffectID]; ok {
					if d.Status == "error" {
						steps[idx].Status = StepFailed
					} else {
						steps[idx].Status = StepDone
					}
					delete(inFlight, d.EffectID)
				} else {
					// fallback: resolve first active/waiting step
					for i := range steps {
						if steps[i].Status == StepActive || steps[i].Status == StepWaiting {
							steps[i].Status = StepDone
							break
						}
					}
				}
			}

		case "RUN_COMPLETED":
			// Mark all steps done
			for i := range steps {
				if steps[i].Status != StepFailed {
					steps[i].Status = StepDone
				}
			}
			completed = true

		case "RUN_FAILED":
			// Mark last active step as failed
			for i := len(steps) - 1; i >= 0; i-- {
				if steps[i].Status == StepActive || steps[i].Status == StepWaiting {
					steps[i].Status = StepFailed
					break
				}
			}
			failed = true
		}
	}

	if len(steps) == 0 {
		return WorkerPipeline{}, false
	}

	return WorkerPipeline{
		WorkerName: runID,
		Parcelle:   parcelle,
		Steps:      steps,
		Completed:  completed,
		Failed:     failed,
	}, true
}

// pickLabel selects the best human-readable label for a step.
func pickLabel(d journalEffectData) string {
	for _, s := range []string{d.Label, d.Title, d.TaskID} {
		if s != "" {
			return s
		}
	}
	return "?"
}
