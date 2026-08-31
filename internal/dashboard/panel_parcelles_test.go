package dashboard

import (
	"os"
	"path/filepath"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// ── Panel interface compliance ───────────────────────────────────────────────

func TestParcellesPanel_PanelInterface(t *testing.T) {
	var _ Panel = (*ParcellesPanel)(nil)
}

func TestNewParcellesPanel(t *testing.T) {
	p := NewParcellesPanel()
	if p == nil {
		t.Fatal("NewParcellesPanel returned nil")
	}
	if p.Title() != "Parcelles" {
		t.Errorf("Title() = %q, want %q", p.Title(), "Parcelles")
	}
}

func TestNewParcellesPanelWithDir(t *testing.T) {
	p := NewParcellesPanelWithDir("/some/dir", nil)
	if p.vignobleDir != "/some/dir" {
		t.Errorf("vignobleDir = %q, want %q", p.vignobleDir, "/some/dir")
	}
}

// ── SetSize / SetFocused ─────────────────────────────────────────────────────

func TestParcellesPanel_SetSize(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.SetSize(100, 15)
	if p.width != 100 || p.height != 15 {
		t.Errorf("SetSize: got %dx%d, want 100x15", p.width, p.height)
	}
}

func TestParcellesPanel_SetFocused(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.SetFocused(true)
	if !p.focused {
		t.Error("SetFocused(true) did not set focused")
	}
	p.SetFocused(false)
	if p.focused {
		t.Error("SetFocused(false) did not clear focused")
	}
}

// ── Init ─────────────────────────────────────────────────────────────────────

func TestParcellesPanel_Init_returnsCmd(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	cmd := p.Init()
	if cmd == nil {
		t.Error("Init() should return a non-nil cmd (batch of load+tick)")
	}
}

// ── View rendering ────────────────────────────────────────────────────────────

func TestParcellesPanel_View_noVignoble(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.SetSize(80, 10)
	v := p.View()
	if !containsAny(v, "PINARD_VIGNOBLE not set") {
		t.Errorf("View() without vignoble should warn, got: %q", v)
	}
}

func TestParcellesPanel_View_empty(t *testing.T) {
	p := NewParcellesPanelWithDir("/nonexistent/vignoble", nil)
	p.SetSize(80, 10)
	// Load returns empty items (dir doesn't exist)
	p.items = nil
	v := p.View()
	if !containsAny(v, "no parcelles") {
		t.Errorf("View() with no items should say '(no parcelles)', got: %q", v)
	}
}

func TestParcellesPanel_View_withError(t *testing.T) {
	p := NewParcellesPanelWithDir("/nonexistent", nil)
	p.vignobleDir = "/nonexistent"
	p.err = "permission denied"
	p.SetSize(80, 10)
	v := p.View()
	if !containsAny(v, "error:", "permission denied") {
		t.Errorf("View() with error should show it, got: %q", v)
	}
}

func TestParcellesPanel_View_containsTitle(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.SetSize(80, 10)
	v := p.View()
	if !containsAny(v, "Parcelles") {
		t.Errorf("View() should contain panel title, got: %q", v)
	}
}

// ── Message handling ─────────────────────────────────────────────────────────

func TestParcellesPanel_parcellesLoadedMsg_ok(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	items := []ParcelleInfo{
		{Name: "exo-cli", WorkerCount: 2, RunCount: 3},
		{Name: "charon", WorkerCount: 0, RunCount: 1},
	}
	updated, _ := p.Update(parcellesLoadedMsg{items: items})
	pp := updated.(*ParcellesPanel)
	if len(pp.items) != 2 {
		t.Errorf("expected 2 items, got %d", len(pp.items))
	}
	if pp.err != "" {
		t.Errorf("expected no error, got %q", pp.err)
	}
}

func TestParcellesPanel_parcellesLoadedMsg_err(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	updated, _ := p.Update(parcellesLoadedMsg{err: "scan failed"})
	pp := updated.(*ParcellesPanel)
	if pp.err != "scan failed" {
		t.Errorf("expected err 'scan failed', got %q", pp.err)
	}
}

func TestParcellesPanel_parcellesLoadedMsg_clampsCursor(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.cursor = 5
	updated, _ := p.Update(parcellesLoadedMsg{items: []ParcelleInfo{{Name: "a"}}})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 0 {
		t.Errorf("cursor should be clamped to 0, got %d", pp.cursor)
	}
}

func TestParcellesPanel_parcellesTickMsg_returnsCmd(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	_, cmd := p.Update(parcellesTickMsg{})
	if cmd == nil {
		t.Error("parcellesTickMsg should return a non-nil cmd")
	}
}

// ── Keyboard navigation ───────────────────────────────────────────────────────

func TestParcellesPanel_ScrollDown(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{
		{Name: "a"},
		{Name: "b"},
		{Name: "c"},
	}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("j")})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 1 {
		t.Errorf("cursor after j: got %d, want 1", pp.cursor)
	}
}

func TestParcellesPanel_ScrollUp(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.cursor = 2
	p.items = []ParcelleInfo{{Name: "a"}, {Name: "b"}, {Name: "c"}}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("k")})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 1 {
		t.Errorf("cursor after k: got %d, want 1", pp.cursor)
	}
}

func TestParcellesPanel_ScrollClamped_top(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.cursor = 0
	p.items = []ParcelleInfo{{Name: "a"}, {Name: "b"}}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("k")})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 0 {
		t.Errorf("cursor shouldn't go below 0, got %d", pp.cursor)
	}
}

func TestParcellesPanel_ScrollClamped_bottom(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.cursor = 1
	p.items = []ParcelleInfo{{Name: "a"}, {Name: "b"}}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("j")})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 1 {
		t.Errorf("cursor shouldn't exceed last index, got %d", pp.cursor)
	}
}

func TestParcellesPanel_Scroll_notFocused(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(false)
	p.items = []ParcelleInfo{{Name: "a"}, {Name: "b"}}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("j")})
	pp := updated.(*ParcellesPanel)
	if pp.cursor != 0 {
		t.Errorf("unfocused panel cursor should not move, got %d", pp.cursor)
	}
}

// ── Active/idle indicators in View ───────────────────────────────────────────

func TestParcellesPanel_View_activeIndicator(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetSize(80, 10)
	p.items = []ParcelleInfo{
		{Name: "exo-cli", WorkerCount: 2, RunCount: 3},
	}
	v := p.View()
	if !containsAny(v, "●") {
		t.Errorf("active parcelle should show ● indicator, got: %q", v)
	}
}

func TestParcellesPanel_View_idleIndicator(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetSize(80, 10)
	p.items = []ParcelleInfo{
		{Name: "charon", WorkerCount: 0, RunCount: 1},
	}
	v := p.View()
	if !containsAny(v, "○") {
		t.Errorf("idle parcelle should show ○ indicator, got: %q", v)
	}
}

func TestParcellesPanel_View_pendingGate(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetSize(80, 10)
	p.items = []ParcelleInfo{
		{Name: "exo-cli", WorkerCount: 1, RunCount: 2, PendingGates: 1},
	}
	v := p.View()
	if !containsAny(v, "pending gate") {
		t.Errorf("parcelle with gate should show 'pending gate', got: %q", v)
	}
}

func TestParcellesPanel_View_noPendingGate(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetSize(80, 10)
	p.items = []ParcelleInfo{
		{Name: "charon", WorkerCount: 0, RunCount: 1, PendingGates: 0},
	}
	v := p.View()
	if containsAny(v, "pending gate") {
		t.Errorf("parcelle without gate should not show 'pending gate', got: %q", v)
	}
}

// ── formatParcelleLine ───────────────────────────────────────────────────────

func TestFormatParcelleLine_active(t *testing.T) {
	item := ParcelleInfo{Name: "exo-cli", WorkerCount: 2, RunCount: 3}
	line := formatParcelleLine(item, 60)
	if !containsAny(line, "●") {
		t.Errorf("active line should contain ●, got: %q", line)
	}
	if !containsAny(line, "2 vendangeurs") {
		t.Errorf("line should contain '2 vendangeurs', got: %q", line)
	}
	if !containsAny(line, "3 runs") {
		t.Errorf("line should contain '3 runs', got: %q", line)
	}
}

func TestFormatParcelleLine_idle(t *testing.T) {
	item := ParcelleInfo{Name: "charon", WorkerCount: 0, RunCount: 1}
	line := formatParcelleLine(item, 60)
	if !containsAny(line, "○") {
		t.Errorf("idle line should contain ○, got: %q", line)
	}
	if !containsAny(line, "0 vendangeurs") {
		t.Errorf("line should contain '0 vendangeurs', got: %q", line)
	}
	if !containsAny(line, "1 run") {
		t.Errorf("line should contain '1 run' (singular), got: %q", line)
	}
}

func TestFormatParcelleLine_singleWorker(t *testing.T) {
	item := ParcelleInfo{Name: "reviews", WorkerCount: 1, RunCount: 2}
	line := formatParcelleLine(item, 60)
	if !containsAny(line, "1 vendangeur") {
		t.Errorf("line should contain '1 vendangeur' (singular), got: %q", line)
	}
}

func TestFormatParcelleLine_pendingGates(t *testing.T) {
	item := ParcelleInfo{Name: "foo", WorkerCount: 1, RunCount: 1, PendingGates: 1}
	line := formatParcelleLine(item, 80)
	if !containsAny(line, "1 pending gate") {
		t.Errorf("line should contain '1 pending gate', got: %q", line)
	}
}

func TestFormatParcelleLine_multiplePendingGates(t *testing.T) {
	item := ParcelleInfo{Name: "foo", WorkerCount: 1, RunCount: 2, PendingGates: 3}
	line := formatParcelleLine(item, 80)
	if !containsAny(line, "3 pending gates") {
		t.Errorf("line should contain '3 pending gates', got: %q", line)
	}
}

func TestFormatParcelleLine_archived(t *testing.T) {
	item := ParcelleInfo{Name: "old-work", Status: "archived"}
	line := formatParcelleLine(item, 60)
	if !containsAny(line, "archived") {
		t.Errorf("archived line should contain '(archived)', got: %q", line)
	}
}

// ── Archived filtering ───────────────────────────────────────────────────────

func TestParcellesPanel_ArchivedHiddenByDefault(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.items = []ParcelleInfo{
		{Name: "active-one", WorkerCount: 1},
		{Name: "old-stuff", Status: "archived"},
	}
	vis := p.visibleItems()
	if len(vis) != 1 {
		t.Fatalf("expected 1 visible item (archived hidden), got %d", len(vis))
	}
	if vis[0].Name != "active-one" {
		t.Errorf("visible item should be 'active-one', got %q", vis[0].Name)
	}
}

func TestParcellesPanel_ToggleArchived(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{
		{Name: "active-one"},
		{Name: "old-stuff", Status: "archived"},
	}

	// Press 'a' to show archived
	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("a")})
	pp := updated.(*ParcellesPanel)
	if !pp.showArchived {
		t.Error("pressing 'a' should toggle showArchived to true")
	}
	vis := pp.visibleItems()
	if len(vis) != 2 {
		t.Errorf("with showArchived=true, expected 2 items, got %d", len(vis))
	}

	// Press 'a' again to hide
	updated, _ = pp.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("a")})
	pp = updated.(*ParcellesPanel)
	if pp.showArchived {
		t.Error("pressing 'a' again should toggle showArchived to false")
	}
}

// ── Delete ───────────────────────────────────────────────────────────────────

func TestParcellesPanel_DeleteConfirm(t *testing.T) {
	dir := t.TempDir()
	mkRun(t, dir, "to-delete", "run1", true)

	p := NewParcellesPanelWithDir(dir, nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{{Name: "to-delete", WorkerCount: 0, RunCount: 1}}

	// Press 'd' → should enter confirm mode
	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("d")})
	pp := updated.(*ParcellesPanel)
	if pp.confirmName != "to-delete" {
		t.Errorf("expected confirmName='to-delete', got %q", pp.confirmName)
	}

	// Confirm view shows prompt
	pp.SetSize(80, 10)
	v := pp.View()
	if !containsAny(v, "delete", "to-delete", "y/n") {
		t.Errorf("confirm view should show delete prompt, got: %q", v)
	}

	// Press 'y' → delete
	updated, cmd := pp.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("y")})
	pp = updated.(*ParcellesPanel)
	if cmd == nil {
		t.Fatal("expected a delete command")
	}
	// Execute the command
	msg := cmd()
	if delMsg, ok := msg.(parcellesDeletedMsg); ok {
		if delMsg.err != "" {
			t.Errorf("unexpected delete error: %s", delMsg.err)
		}
	} else {
		t.Errorf("expected parcellesDeletedMsg, got %T", msg)
	}

	// Verify directory removed
	if _, err := os.Stat(filepath.Join(dir, "parcelles", "to-delete")); !os.IsNotExist(err) {
		t.Error("parcelle directory should have been deleted")
	}
}

func TestParcellesPanel_DeleteCancel(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{{Name: "keep-me", WorkerCount: 0}}
	p.confirmName = "keep-me"

	// Press 'n' → cancel
	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("n")})
	pp := updated.(*ParcellesPanel)
	if pp.confirmName != "" {
		t.Errorf("pressing 'n' should clear confirmName, got %q", pp.confirmName)
	}
}

func TestParcellesPanel_DeleteBlockedByActiveWorker(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{{Name: "busy", WorkerCount: 2}}

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("d")})
	pp := updated.(*ParcellesPanel)
	if pp.confirmName != "" {
		t.Error("should not enter confirm mode for active parcelle")
	}
	if pp.err == "" {
		t.Error("should set error when trying to delete active parcelle")
	}
}

// ── Enter to select ──────────────────────────────────────────────────────────

func TestParcellesPanel_EnterSelects(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetFocused(true)
	p.items = []ParcelleInfo{{Name: "my-parcelle"}, {Name: "other"}}
	p.cursor = 0

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyEnter})
	pp := updated.(*ParcellesPanel)
	if pp.selected != "my-parcelle" {
		t.Errorf("enter should select 'my-parcelle', got %q", pp.selected)
	}
}

func TestParcellesPanel_SelectedShownInView(t *testing.T) {
	p := NewParcellesPanelWithDir("/vig", nil)
	p.SetSize(80, 10)
	p.items = []ParcelleInfo{{Name: "exo-cli"}, {Name: "charon"}}
	p.selected = "exo-cli"
	v := p.View()
	if !containsAny(v, "*") {
		t.Errorf("selected parcelle should show * indicator, got: %q", v)
	}
}

// ── scanParcelles ─────────────────────────────────────────────────────────────

func TestScanParcelles_nonexistentDir(t *testing.T) {
	items, err := scanParcelles("/nonexistent/vignoble")
	if err != nil {
		t.Errorf("scanParcelles on nonexistent dir should not error, got: %v", err)
	}
	if len(items) != 0 {
		t.Errorf("expected 0 items, got %d", len(items))
	}
}

func TestScanParcelles_emptyVignoble(t *testing.T) {
	items, err := scanParcelles("")
	if err != nil {
		t.Errorf("scanParcelles with empty dir should not error, got: %v", err)
	}
	if items != nil {
		t.Errorf("expected nil items for empty dir, got %v", items)
	}
}

func TestScanParcelles_withParcelles(t *testing.T) {
	dir := t.TempDir()
	// Create vignoble/parcelles/exo-cli/runs/run1/journal/001.json
	mkRun(t, dir, "exo-cli", "run1", false) // active
	mkRun(t, dir, "exo-cli", "run2", true)  // completed
	mkRun(t, dir, "charon", "run1", true)   // completed

	items, err := scanParcelles(dir)
	if err != nil {
		t.Fatalf("scanParcelles error: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("expected 2 parcelles, got %d", len(items))
	}

	// exo-cli has 1 active worker → sorted first
	if items[0].Name != "exo-cli" {
		t.Errorf("first item should be exo-cli (has workers), got %q", items[0].Name)
	}
	if items[0].WorkerCount != 1 {
		t.Errorf("exo-cli WorkerCount = %d, want 1", items[0].WorkerCount)
	}
	if items[0].RunCount != 2 {
		t.Errorf("exo-cli RunCount = %d, want 2", items[0].RunCount)
	}

	if items[1].Name != "charon" {
		t.Errorf("second item should be charon, got %q", items[1].Name)
	}
	if items[1].WorkerCount != 0 {
		t.Errorf("charon WorkerCount = %d, want 0", items[1].WorkerCount)
	}
}

func TestScanParcelles_archivedStatus(t *testing.T) {
	dir := t.TempDir()
	mkRun(t, dir, "active-one", "run1", false)
	mkRun(t, dir, "old-stuff", "run1", true)

	// Mark old-stuff as archived
	yamlPath := filepath.Join(dir, "parcelles", "old-stuff", "parcelle.yaml")
	os.WriteFile(yamlPath, []byte("name: old-stuff\nstatus: archived\n"), 0o644)

	items, err := scanParcelles(dir)
	if err != nil {
		t.Fatalf("scanParcelles error: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("expected 2 parcelles (scan returns all), got %d", len(items))
	}

	// Active should sort first, archived last
	if items[0].Name != "active-one" {
		t.Errorf("first should be active-one, got %q", items[0].Name)
	}
	if items[1].Name != "old-stuff" {
		t.Errorf("second should be old-stuff, got %q", items[1].Name)
	}
	if items[1].Status != "archived" {
		t.Errorf("old-stuff should have Status='archived', got %q", items[1].Status)
	}
}

func TestScanParcelles_pendingGate(t *testing.T) {
	dir := t.TempDir()
	mkRun(t, dir, "babysitter", "run1", false) // active
	// Add a gate file to run1
	stateDir := filepath.Join(dir, "parcelles", "babysitter", "runs", "run1", "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stateDir, "gate-abc.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	items, err := scanParcelles(dir)
	if err != nil {
		t.Fatalf("scanParcelles error: %v", err)
	}
	if len(items) != 1 {
		t.Fatalf("expected 1 parcelle, got %d", len(items))
	}
	if items[0].PendingGates != 1 {
		t.Errorf("PendingGates = %d, want 1", items[0].PendingGates)
	}
}

// ── buildParcelleInfo ─────────────────────────────────────────────────────────

func TestBuildParcelleInfo_noRunsDir(t *testing.T) {
	dir := t.TempDir()
	parcelleDir := filepath.Join(dir, "myparcelle")
	if err := os.MkdirAll(parcelleDir, 0o755); err != nil {
		t.Fatal(err)
	}
	info := buildParcelleInfo("myparcelle", parcelleDir)
	if info.Name != "myparcelle" {
		t.Errorf("Name = %q, want %q", info.Name, "myparcelle")
	}
	if info.RunCount != 0 {
		t.Errorf("RunCount = %d, want 0", info.RunCount)
	}
}

// ── isRunActive ───────────────────────────────────────────────────────────────

func TestIsRunActive_noJournalDir(t *testing.T) {
	dir := t.TempDir()
	if isRunActive(dir) {
		t.Error("run with no journal dir should not be active")
	}
}

func TestIsRunActive_emptyJournal(t *testing.T) {
	dir := t.TempDir()
	journalDir := filepath.Join(dir, "journal")
	if err := os.MkdirAll(journalDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if isRunActive(dir) {
		t.Error("run with empty journal should not be active")
	}
}

func TestIsRunActive_activeRun(t *testing.T) {
	dir := t.TempDir()
	writeJournalEntry(t, dir, "001.json", `{"type":"EFFECT_REQUESTED","data":{}}`)
	if !isRunActive(dir) {
		t.Error("run with EFFECT_REQUESTED but no terminal event should be active")
	}
}

func TestIsRunActive_completedRun(t *testing.T) {
	dir := t.TempDir()
	writeJournalEntry(t, dir, "001.json", `{"type":"EFFECT_REQUESTED","data":{}}`)
	writeJournalEntry(t, dir, "002.json", `{"type":"RUN_COMPLETED","data":{}}`)
	if isRunActive(dir) {
		t.Error("run with RUN_COMPLETED should not be active")
	}
}

func TestIsRunActive_failedRun(t *testing.T) {
	dir := t.TempDir()
	writeJournalEntry(t, dir, "001.json", `{"type":"EFFECT_REQUESTED","data":{}}`)
	writeJournalEntry(t, dir, "002.json", `{"type":"RUN_FAILED","data":{}}`)
	if isRunActive(dir) {
		t.Error("run with RUN_FAILED should not be active")
	}
}

// ── hasPendingGate ────────────────────────────────────────────────────────────

func TestHasPendingGate_noGate(t *testing.T) {
	dir := t.TempDir()
	if hasPendingGate(dir) {
		t.Error("run with no gate files should return false")
	}
}

func TestHasPendingGate_gateInState(t *testing.T) {
	dir := t.TempDir()
	stateDir := filepath.Join(dir, "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stateDir, "gate-xyz.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !hasPendingGate(dir) {
		t.Error("run with gate file in state/ should return true")
	}
}

func TestHasPendingGate_gateInTasks(t *testing.T) {
	dir := t.TempDir()
	tasksDir := filepath.Join(dir, "tasks")
	if err := os.MkdirAll(tasksDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tasksDir, "gate-abc.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !hasPendingGate(dir) {
		t.Error("run with gate file in tasks/ should return true")
	}
}

func TestHasPendingGate_nonGateFile(t *testing.T) {
	dir := t.TempDir()
	stateDir := filepath.Join(dir, "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stateDir, "output.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if hasPendingGate(dir) {
		t.Error("non-gate file should not trigger hasPendingGate")
	}
}

// ── truncateStr ───────────────────────────────────────────────────────────────

func TestTruncateStr_short(t *testing.T) {
	if got := truncateStr("abc", 10); got != "abc" {
		t.Errorf("truncateStr short: got %q, want %q", got, "abc")
	}
}

func TestTruncateStr_exact(t *testing.T) {
	if got := truncateStr("abcde", 5); got != "abcde" {
		t.Errorf("truncateStr exact: got %q, want %q", got, "abcde")
	}
}

func TestTruncateStr_over(t *testing.T) {
	got := truncateStr("abcdefgh", 5)
	if len([]rune(got)) != 5 {
		t.Errorf("truncateStr over: got len %d, want 5; value %q", len([]rune(got)), got)
	}
	if got[len(got)-3:] != "…" {
		t.Errorf("truncateStr over should end with …, got %q", got)
	}
}

// ── visibleItems ─────────────────────────────────────────────────────────────

func TestVisibleItems_filtersArchived(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.items = []ParcelleInfo{
		{Name: "a"},
		{Name: "b", Status: "archived"},
		{Name: "c"},
	}
	vis := p.visibleItems()
	if len(vis) != 2 {
		t.Fatalf("expected 2 visible items, got %d", len(vis))
	}
	for _, item := range vis {
		if item.isArchived() {
			t.Error("archived item should not appear in visibleItems when showArchived=false")
		}
	}
}

func TestVisibleItems_showsAllWhenToggled(t *testing.T) {
	p := NewParcellesPanelWithDir("", nil)
	p.showArchived = true
	p.items = []ParcelleInfo{
		{Name: "a"},
		{Name: "b", Status: "archived"},
	}
	vis := p.visibleItems()
	if len(vis) != 2 {
		t.Errorf("expected 2 visible items with showArchived=true, got %d", len(vis))
	}
}

// ── extractVignoble ──────────────────────────────────────────────────────────

func TestExtractVignoble(t *testing.T) {
	tests := []struct {
		dir  string
		want string
	}{
		{"/home/user/vignoble-exohub", "exohub"},
		{"/home/user/vignoble-data", "data"},
		{"/home/user/mydir", "mydir"},
		{"", ""},
	}
	for _, tt := range tests {
		got := extractVignoble(tt.dir)
		if got != tt.want {
			t.Errorf("extractVignoble(%q) = %q, want %q", tt.dir, got, tt.want)
		}
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// mkRun creates a minimal run directory structure.
// If completed=true, the journal contains a RUN_COMPLETED entry.
func mkRun(t *testing.T, vignobleDir, parcelle, runID string, completed bool) {
	t.Helper()
	journalDir := filepath.Join(vignobleDir, "parcelles", parcelle, "runs", runID, "journal")
	if err := os.MkdirAll(journalDir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeJournalEntry(t, filepath.Join(vignobleDir, "parcelles", parcelle, "runs", runID),
		"001.json", `{"type":"EFFECT_REQUESTED","data":{}}`)
	if completed {
		writeJournalEntry(t, filepath.Join(vignobleDir, "parcelles", parcelle, "runs", runID),
			"002.json", `{"type":"RUN_COMPLETED","data":{}}`)
	}
}

// writeJournalEntry writes a JSON file to runDir/journal/name.
func writeJournalEntry(t *testing.T, runDir, name, content string) {
	t.Helper()
	journalDir := filepath.Join(runDir, "journal")
	if err := os.MkdirAll(journalDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(journalDir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
