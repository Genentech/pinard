package dashboard

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/Genentech/pinard/internal/state"
)

func TestFormatMRLine_CIPassing(t *testing.T) {
	mr := &state.WatchedMR{
		MR:               128,
		Project:          "exo-cli",
		LastPipelineID:   999,
		AutoMergeLabeled: true,
	}
	got := FormatMRLine(mr)
	want := "!128   exo-cli     ✓ CI  auto-merge"
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestFormatMRLine_CIFailing(t *testing.T) {
	mr := &state.WatchedMR{
		MR:                310,
		Project:           "charon",
		PipelineFailCount: 2,
	}
	got := FormatMRLine(mr)
	want := "!310   charon      ✗ CI  attempt 2/5"
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestFormatMRLine_NeedsApproval(t *testing.T) {
	mr := &state.WatchedMR{
		MR:                    128,
		Project:               "exo-cli",
		LastPipelineID:        999,
		NeedsApprovalNotified: true,
		AutoMergeLabeled:      true,
	}
	got := FormatMRLine(mr)
	want := "!128   exo-cli     ✓ CI  ⏳ approval  auto-merge"
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestFormatMRLine_PostMerge(t *testing.T) {
	mr := &state.WatchedMR{
		MR:              77,
		Project:         "myproject",
		State:           "post_merge",
		PostMergeChecks: 3,
	}
	got := FormatMRLine(mr)
	want := "!77    myproject   post-merge check 3/10"
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestFormatMRLine_Merged(t *testing.T) {
	mr := &state.WatchedMR{
		MR:      42,
		Project: "repo",
		State:   "merged",
	}
	got := FormatMRLine(mr)
	want := "!42    repo        ✓ merged"
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestFormatMRLine_NoCI(t *testing.T) {
	mr := &state.WatchedMR{
		MR:      55,
		Project: "proj",
	}
	got := FormatMRLine(mr)
	want := "!55    proj      "
	if got != want {
		t.Errorf("FormatMRLine() = %q, want %q", got, want)
	}
}

func TestBuildMRRows_Empty(t *testing.T) {
	rows := buildMRRows(nil)
	if len(rows) != 0 {
		t.Errorf("expected 0 rows, got %d", len(rows))
	}
}

func TestBuildMRRows_FiltersZeroIID(t *testing.T) {
	watched := map[string]*state.WatchedMR{
		"no-iid":  {MR: 0, Project: "x"},
		"has-iid": {MR: 1, Project: "y"},
	}
	rows := buildMRRows(watched)
	if len(rows) != 1 {
		t.Errorf("expected 1 row, got %d", len(rows))
	}
	if rows[0].iid != 1 {
		t.Errorf("expected iid=1, got %d", rows[0].iid)
	}
}

func TestBuildMRRows_SortedByIID(t *testing.T) {
	watched := map[string]*state.WatchedMR{
		"b": {MR: 310, Project: "charon"},
		"a": {MR: 128, Project: "exo-cli"},
		"c": {MR: 77, Project: "other"},
	}
	rows := buildMRRows(watched)
	if len(rows) != 3 {
		t.Fatalf("expected 3 rows, got %d", len(rows))
	}
	if rows[0].iid != 77 || rows[1].iid != 128 || rows[2].iid != 310 {
		t.Errorf("rows not sorted by IID: got %v %v %v",
			rows[0].iid, rows[1].iid, rows[2].iid)
	}
}

func TestMRsPanelInit(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	if p == nil {
		t.Fatal("NewMRsPanelWithPath returned nil")
	}
	if p.Title() != "MRs" {
		t.Errorf("Title() = %q, want %q", p.Title(), "MRs")
	}
}

func TestMRsPanelSetSize(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	p.SetSize(80, 20)
	if p.width != 80 || p.height != 20 {
		t.Errorf("SetSize(80, 20): got width=%d height=%d", p.width, p.height)
	}
}

func TestMRsPanelFocusBlur(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	if p.focused {
		t.Error("panel should start unfocused")
	}
	p.SetFocused(true)
	if !p.focused {
		t.Error("panel should be focused after SetFocused(true)")
	}
	p.SetFocused(false)
	if p.focused {
		t.Error("panel should be unfocused after SetFocused(false)")
	}
}

func TestMRsPanelScrolling(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	p.SetFocused(true)
	p.rows = []mrRow{
		{iid: 1, project: "a", line: "!1    a"},
		{iid: 2, project: "b", line: "!2    b"},
		{iid: 3, project: "c", line: "!3    c"},
	}

	// Cursor starts at 0; 'j' moves down
	panel, _ := p.Update(keyMsg("j"))
	mp := panel.(*MRsPanel)
	if mp.cursor != 1 {
		t.Errorf("cursor after j: got %d, want 1", mp.cursor)
	}

	// 'k' moves up
	panel, _ = mp.Update(keyMsg("k"))
	mp = panel.(*MRsPanel)
	if mp.cursor != 0 {
		t.Errorf("cursor after k: got %d, want 0", mp.cursor)
	}

	// Can't go above 0
	panel, _ = mp.Update(keyMsg("k"))
	mp = panel.(*MRsPanel)
	if mp.cursor != 0 {
		t.Errorf("cursor shouldn't go below 0, got %d", mp.cursor)
	}
}

func TestMRsPanelScrollingNotFocused(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	p.SetFocused(false)
	p.rows = []mrRow{
		{iid: 1, project: "a", line: "!1    a"},
		{iid: 2, project: "b", line: "!2    b"},
	}

	panel, _ := p.Update(keyMsg("j"))
	mp := panel.(*MRsPanel)
	if mp.cursor != 0 {
		t.Errorf("unfocused panel cursor moved: got %d, want 0", mp.cursor)
	}
}

func TestMRsPanelLoadedMsg(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	rows := []mrRow{{iid: 5, project: "x", line: "!5    x"}}

	panel, _ := p.Update(mrsLoadedMsg{rows: rows})
	mp := panel.(*MRsPanel)
	if len(mp.rows) != 1 {
		t.Errorf("expected 1 row after load, got %d", len(mp.rows))
	}
	if mp.err != "" {
		t.Errorf("expected no error, got %q", mp.err)
	}
}

func TestMRsPanelLoadedMsgError(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	panel, _ := p.Update(mrsLoadedMsg{err: "some error"})
	mp := panel.(*MRsPanel)
	if mp.err != "some error" {
		t.Errorf("expected error %q, got %q", "some error", mp.err)
	}
}

func TestMRsPanelViewEmpty(t *testing.T) {
	p := NewMRsPanelWithPath("/nonexistent/mr-watcher.yaml")
	p.SetSize(50, 10)
	view := p.View()
	if view == "" {
		t.Error("View() returned empty string")
	}
	if !findStr(view, "MRs") {
		t.Error("View() does not contain panel title 'MRs'")
	}
}

// keyMsg creates a tea.KeyMsg for testing.
func keyMsg(key string) tea.KeyMsg {
	return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(key)}
}

func findStr(s, sub string) bool {
	for i := 0; i <= len(s)-len(sub); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
