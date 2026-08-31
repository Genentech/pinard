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
	"github.com/nats-io/nats.go"

	"github.com/Genentech/pinard/internal/config"
)

const (
	parcellesPollInterval = 10 * time.Second

	iconActive = "●"
	iconIdle   = "○"
)

// ParcelleKind distinguishes real cross-cutting workstreams from the 1:1
// parcelle/repo buckets the spawn path auto-creates (parcelle name == vigne name).
type ParcelleKind int

const (
	KindWorkstream ParcelleKind = iota // real workstream (name is not a vigne)
	KindRepo                           // 1:1 default (name matches a vigne in vignes.yaml)
)

func (k ParcelleKind) header() string {
	if k == KindRepo {
		return "🌱 Repos"
	}
	return "🌿 Workstreams"
}

// ParcelleInfo holds the aggregated state of a single parcelle.
type ParcelleInfo struct {
	Name         string
	Status       string // "active", "archived", or "" (treated as active)
	Kind         ParcelleKind
	WorkerCount  int // runs without RUN_COMPLETED/RUN_FAILED (active workers)
	RunCount     int // total run dirs
	PendingGates int // runs with pending gate files
}

func (p ParcelleInfo) isArchived() bool {
	return p.Status == "archived"
}

// parcellesLoadedMsg is sent when the directory scan completes.
type parcellesLoadedMsg struct {
	items []ParcelleInfo
	err   string
}

// parcellesTickMsg triggers the next poll.
type parcellesTickMsg struct{}

// parcellesDeletedMsg is sent after a parcelle directory is removed.
type parcellesDeletedMsg struct{ err string }

// ParcellesPanel shows active workstreams (parcelles) from the vignoble dir.
type ParcellesPanel struct {
	vignobleDir string
	vignoble    string // vignoble name (for NATS subjects)
	nc          *nats.Conn

	items        []ParcelleInfo
	cursor       int
	focused      bool
	width        int
	height       int
	err          string
	showArchived bool
	selected     string // currently selected parcelle name
	confirmName  string // non-empty while confirming delete
}

// NewParcellesPanel creates a ParcellesPanel. vignobleDir may be empty — the
// panel then reads PINARD_VIGNOBLE from the environment (consistent with how
// PipelinesPanel works).
func NewParcellesPanel() *ParcellesPanel {
	dir := os.Getenv("PINARD_VIGNOBLE")
	return &ParcellesPanel{
		vignobleDir: dir,
		vignoble:    extractVignoble(dir),
		width:       80,
		height:      6,
	}
}

// NewParcellesPanelWithDir creates a ParcellesPanel rooted at vignobleDir.
func NewParcellesPanelWithDir(vignobleDir string, nc *nats.Conn) *ParcellesPanel {
	return &ParcellesPanel{
		vignobleDir: vignobleDir,
		vignoble:    extractVignoble(vignobleDir),
		nc:          nc,
		width:       80,
		height:      6,
	}
}

func extractVignoble(dir string) string {
	if dir == "" {
		return ""
	}
	parts := strings.Split(dir, "/")
	last := parts[len(parts)-1]
	return strings.TrimPrefix(last, "vignoble-")
}

// ── Panel interface ──────────────────────────────────────────────────────────

func (p *ParcellesPanel) Title() string { return "Parcelles" }

func (p *ParcellesPanel) SetFocused(focused bool) { p.focused = focused }

func (p *ParcellesPanel) SetSize(w, h int) {
	p.width = w
	p.height = h
}

func (p *ParcellesPanel) Init() tea.Cmd {
	return tea.Batch(p.load(), p.tick())
}

func (p *ParcellesPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	switch msg := msg.(type) {
	case parcellesLoadedMsg:
		if msg.err != "" {
			p.err = msg.err
		} else {
			p.err = ""
			p.items = msg.items
			p.clampCursor()
		}
		return p, nil

	case parcellesTickMsg:
		return p, tea.Batch(p.load(), p.tick())

	case parcellesDeletedMsg:
		if msg.err != "" {
			p.err = msg.err
		}
		p.confirmName = ""
		return p, p.load()

	case tea.KeyMsg:
		if !p.focused {
			return p, nil
		}

		// Confirm-delete mode: only y/n/escape
		if p.confirmName != "" {
			switch msg.String() {
			case "y":
				name := p.confirmName
				dir := p.vignobleDir
				return p, func() tea.Msg {
					err := os.RemoveAll(filepath.Join(dir, "parcelles", name))
					if err != nil {
						return parcellesDeletedMsg{err: err.Error()}
					}
					return parcellesDeletedMsg{}
				}
			case "n", "escape", "esc":
				p.confirmName = ""
			}
			return p, nil
		}

		vis := p.visibleItems()
		switch msg.String() {
		case "j", "down":
			if p.cursor < len(vis)-1 {
				p.cursor++
			}
		case "k", "up":
			if p.cursor > 0 {
				p.cursor--
			}
		case "a":
			p.showArchived = !p.showArchived
			p.clampCursor()
		case "d":
			if len(vis) > 0 && p.cursor < len(vis) {
				item := vis[p.cursor]
				if item.WorkerCount > 0 {
					p.err = fmt.Sprintf("cannot delete %q — %d active vendangeur(s)", item.Name, item.WorkerCount)
				} else {
					p.confirmName = item.Name
				}
			}
		case "enter":
			if len(vis) > 0 && p.cursor < len(vis) {
				item := vis[p.cursor]
				p.selected = item.Name
				p.publishSelection(item.Name)
			}
		}
		return p, nil
	}

	return p, nil
}

func (p *ParcellesPanel) visibleItems() []ParcelleInfo {
	if p.showArchived {
		return p.items
	}
	var out []ParcelleInfo
	for _, item := range p.items {
		if !item.isArchived() {
			out = append(out, item)
		}
	}
	return out
}

func (p *ParcellesPanel) clampCursor() {
	vis := p.visibleItems()
	if p.cursor >= len(vis) {
		p.cursor = max(0, len(vis)-1)
	}
}

func (p *ParcellesPanel) publishSelection(name string) {
	if p.nc == nil || p.vignoble == "" {
		return
	}
	payload, _ := json.Marshal(map[string]string{"parcelle": name})
	p.nc.Publish(fmt.Sprintf("pinard.%s.dashboard.parcelle_selected", p.vignoble), payload)
}

func (p *ParcellesPanel) View() string {
	var content string
	switch {
	case p.vignobleDir == "":
		content = dimStyle.Render("PINARD_VIGNOBLE not set")
	case p.err != "":
		content = dimStyle.Render("error: " + p.err)
	case p.confirmName != "":
		content = fmt.Sprintf("delete %q? (y/n)", p.confirmName)
	default:
		vis := p.visibleItems()
		if len(vis) == 0 {
			content = dimStyle.Render("(no parcelles)")
		} else {
			content = p.renderItems(vis)
		}
	}
	return renderPanel(p.Title(), content, p.focused, p.width, p.height)
}

// ── Rendering ────────────────────────────────────────────────────────────────

var (
	activeIndicatorStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("2"))   // green
	idleIndicatorStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("240")) // dim
	gateStyle             = lipgloss.NewStyle().Foreground(lipgloss.Color("214")) // amber
	archivedStyle         = lipgloss.NewStyle().Foreground(lipgloss.Color("240")) // dim
	sectionHeaderStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("245")).Bold(true)
	selectedIndicator     = lipgloss.NewStyle().Foreground(lipgloss.Color("2")).Render("*")
)

func (p *ParcellesPanel) renderItems(vis []ParcelleInfo) string {
	innerH := p.height - 4 // border(2) + title(1) + bottom padding(1)
	if innerH < 1 {
		innerH = 1
	}
	innerW := p.width - 4
	if innerW < 10 {
		innerW = 10
	}

	// Build the display buffer: 🌿/🌱 section headers interleaved with item
	// lines. Headers are rendered-only — the cursor moves over items (vis)
	// alone, so navigation is unaffected. cursorLine tracks where the current
	// item lands in the buffer so the scroll window can keep it visible.
	type dline struct {
		text     string
		isHeader bool
	}
	buf := make([]dline, 0, len(vis)+2)
	cursorLine := 0
	firstKind := true
	var lastKind ParcelleKind
	for i, item := range vis {
		if firstKind || item.Kind != lastKind {
			buf = append(buf, dline{text: sectionHeaderStyle.Render(item.Kind.header()), isHeader: true})
			lastKind = item.Kind
			firstKind = false
		}

		sel := " "
		if item.Name == p.selected {
			sel = selectedIndicator
		}
		var text string
		if i == p.cursor && p.focused {
			text = lipgloss.NewStyle().
				Foreground(lipgloss.Color("62")).
				Bold(true).
				Render("> " + sel + " " + formatParcelleLine(item, innerW-4))
		} else {
			text = "  " + sel + " " + formatParcelleLine(item, innerW-4)
		}
		if i == p.cursor {
			cursorLine = len(buf)
		}
		buf = append(buf, dline{text: text})
	}

	// Scroll window over display lines, keeping the cursor line in view.
	start := 0
	if cursorLine >= innerH {
		start = cursorLine - innerH + 1
	}
	end := start + innerH
	if end > len(buf) {
		end = len(buf)
		start = max(0, end-innerH)
	}

	lines := make([]string, 0, end-start+1)
	for i := start; i < end; i++ {
		lines = append(lines, buf[i].text)
	}

	if len(buf) > innerH {
		indicator := fmt.Sprintf(" %d/%d", p.cursor+1, len(vis))
		lines = append(lines, dimStyle.Render(indicator))
	}

	return strings.Join(lines, "\n")
}

// formatParcelleLine formats a single parcelle row.
// Example: "● exo-cli       2 workers  3 runs  1 pending gate"
func formatParcelleLine(item ParcelleInfo, maxWidth int) string {
	if item.isArchived() {
		nameField := fmt.Sprintf("%-14s", truncateStr(item.Name, 14))
		return archivedStyle.Render(fmt.Sprintf("%s %s  (archived)", iconIdle, nameField))
	}

	var icon string
	if item.WorkerCount > 0 {
		icon = activeIndicatorStyle.Render(iconActive)
	} else {
		icon = idleIndicatorStyle.Render(iconIdle)
	}

	// Build the text portion (without ANSI from the icon)
	nameField := fmt.Sprintf("%-14s", truncateStr(item.Name, 14))

	workerLabel := "vendangeurs"
	if item.WorkerCount == 1 {
		workerLabel = "vendangeur"
	}
	runLabel := "runs"
	if item.RunCount == 1 {
		runLabel = "run"
	}

	text := fmt.Sprintf("%s  %d %-7s  %d %s",
		nameField,
		item.WorkerCount, workerLabel,
		item.RunCount, runLabel,
	)

	if item.PendingGates > 0 {
		gateText := fmt.Sprintf("  %d pending gate", item.PendingGates)
		if item.PendingGates > 1 {
			gateText += "s"
		}
		text += gateStyle.Render(gateText)
	}

	// icon + space + text
	return icon + " " + text
}

func truncateStr(s string, max int) string {
	runes := []rune(s)
	if len(runes) <= max {
		return s
	}
	if max <= 1 {
		return "…"
	}
	return string(runes[:max-1]) + "…"
}

// ── Data loading ─────────────────────────────────────────────────────────────

func (p *ParcellesPanel) load() tea.Cmd {
	dir := p.vignobleDir
	return func() tea.Msg {
		items, err := scanParcelles(dir)
		if err != nil {
			return parcellesLoadedMsg{err: err.Error()}
		}
		return parcellesLoadedMsg{items: items}
	}
}

func (p *ParcellesPanel) tick() tea.Cmd {
	return tea.Tick(parcellesPollInterval, func(time.Time) tea.Msg {
		return parcellesTickMsg{}
	})
}

// scanParcelles reads vignobleDir/parcelles/*/ and returns a sorted slice of
// ParcelleInfo. Each parcelle's stats are derived from its runs/ subdirectory.
func scanParcelles(vignobleDir string) ([]ParcelleInfo, error) {
	if vignobleDir == "" {
		return nil, nil
	}
	parcellesDir := filepath.Join(vignobleDir, "parcelles")
	entries, err := os.ReadDir(parcellesDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	vignes := vigneNames(vignobleDir)

	var items []ParcelleInfo
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		info := buildParcelleInfo(e.Name(), filepath.Join(parcellesDir, e.Name()))
		if vignes[info.Name] {
			info.Kind = KindRepo
		}
		items = append(items, info)
	}

	sort.Slice(items, func(i, j int) bool {
		// Workstreams before repos; within a kind: active, then idle, then
		// archived; alphabetical within each group.
		if items[i].Kind != items[j].Kind {
			return items[i].Kind < items[j].Kind
		}
		gi, gj := sortGroup(items[i]), sortGroup(items[j])
		if gi != gj {
			return gi < gj
		}
		return items[i].Name < items[j].Name
	})

	return items, nil
}

// vigneNames reads vignes.yaml at the vignoble root and returns the set of vigne
// names. A parcelle whose name matches a vigne is a 1:1 default (KindRepo).
// Missing/unreadable config yields an empty set — all parcelles are workstreams.
func vigneNames(vignobleDir string) map[string]bool {
	out := map[string]bool{}
	cfg, err := config.LoadVignoble(filepath.Join(vignobleDir, "vignes.yaml"))
	if err != nil {
		return out
	}
	for name := range cfg.Vignes {
		out[name] = true
	}
	return out
}

func sortGroup(item ParcelleInfo) int {
	if item.isArchived() {
		return 2
	}
	if item.WorkerCount > 0 {
		return 0
	}
	return 1
}

// buildParcelleInfo computes stats for one parcelle directory.
func buildParcelleInfo(name, parcelleDir string) ParcelleInfo {
	info := ParcelleInfo{Name: name}

	// Read status from parcelle.yaml
	yamlPath := filepath.Join(parcelleDir, "parcelle.yaml")
	if data, err := os.ReadFile(yamlPath); err == nil {
		if strings.Contains(string(data), "status: archived") {
			info.Status = "archived"
		}
	}

	runsDir := filepath.Join(parcelleDir, "runs")
	runs, err := os.ReadDir(runsDir)
	if err != nil {
		return info
	}

	for _, r := range runs {
		if !r.IsDir() {
			continue
		}
		info.RunCount++
		runDir := filepath.Join(runsDir, r.Name())

		if isRunActive(runDir) {
			info.WorkerCount++
		}
		if hasPendingGate(runDir) {
			info.PendingGates++
		}
	}

	return info
}

// isRunActive returns true when the run has no RUN_COMPLETED or RUN_FAILED
// journal entry — i.e., it is still in progress.
func isRunActive(runDir string) bool {
	journalDir := filepath.Join(runDir, "journal")
	entries, err := os.ReadDir(journalDir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(journalDir, e.Name()))
		if err != nil {
			continue
		}
		// Quick string scan avoids full JSON parse on every file
		s := string(data)
		if strings.Contains(s, `"RUN_COMPLETED"`) || strings.Contains(s, `"RUN_FAILED"`) {
			return false
		}
	}
	// Has journal entries but no terminal event → active
	return len(entries) > 0
}

// hasPendingGate returns true when the run contains a pending gate file.
// Gate files are named gate-<id>.json and live in state/ or tasks/.
func hasPendingGate(runDir string) bool {
	for _, sub := range []string{"state", "tasks"} {
		dir := filepath.Join(runDir, sub)
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if !e.IsDir() && strings.HasPrefix(e.Name(), "gate-") {
				return true
			}
		}
	}
	return false
}
