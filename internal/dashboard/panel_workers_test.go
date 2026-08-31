package dashboard

import (
	"fmt"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/nats-io/nats.go"
)

// ── WorkerEntry helpers ───────────────────────────────────────────────────────

func TestStatusIcon(t *testing.T) {
	cases := []struct {
		tempo string
		want  string
	}{
		{"active", IconActive},
		{"completed", IconCompleted},
		{"failed", IconFailed},
		{"awaiting", IconAwaiting},
		{"", IconAwaiting},
		{"unknown", IconAwaiting},
	}
	for _, tc := range cases {
		w := WorkerEntry{Tempo: tc.tempo}
		got := w.StatusIcon()
		if got != tc.want {
			t.Errorf("StatusIcon(%q) = %q, want %q", tc.tempo, got, tc.want)
		}
	}
}

func TestStatusText_withStep(t *testing.T) {
	w := WorkerEntry{Tempo: "active", Step: "implement"}
	text := w.StatusText()
	if text != "🔵 implement" {
		t.Errorf("StatusText() = %q, want %q", text, "🔵 implement")
	}
}

func TestStatusText_withoutStep(t *testing.T) {
	w := WorkerEntry{Tempo: "active"}
	text := w.StatusText()
	if text != "🔵 active" {
		t.Errorf("StatusText() = %q, want %q", text, "🔵 active")
	}
}

func TestStatusText_emptyTempo(t *testing.T) {
	w := WorkerEntry{}
	text := w.StatusText()
	if text != "🟡 awaiting events" {
		t.Errorf("StatusText() = %q, want %q", text, "🟡 awaiting events")
	}
}

func TestStatusText_withProcessEmoji(t *testing.T) {
	w := WorkerEntry{Tempo: "active", Step: "open-mr", Process: "swe"}
	if text := w.StatusText(); text != "🛠️ 🔵 open-mr" {
		t.Errorf("StatusText() = %q, want %q", text, "🛠️ 🔵 open-mr")
	}
}

// ── WorkersPanel construction ─────────────────────────────────────────────────

func TestNewWorkersPanel(t *testing.T) {
	p := NewWorkersPanel("testvig", nil)
	if p == nil {
		t.Fatal("NewWorkersPanel returned nil")
	}
	if p.Title() != "🧺 Vendangeurs" {
		t.Errorf("Title() = %q, want %q", p.Title(), "🧺 Vendangeurs")
	}
}

func TestWorkersPanel_PanelInterface(t *testing.T) {
	var _ Panel = (*WorkersPanel)(nil)
}

func TestWorkersPanel_Init_nilKV(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	cmd := p.Init()
	if cmd != nil {
		t.Error("Init with nil KV should return nil cmd")
	}
}

func TestWorkersPanel_SetSize(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetSize(80, 20)
	if p.width != 80 || p.height != 20 {
		t.Errorf("SetSize did not set dimensions: got %dx%d", p.width, p.height)
	}
}

func TestWorkersPanel_SetFocused(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetFocused(true)
	if !p.focused {
		t.Error("SetFocused(true) did not set p.focused")
	}
	p.SetFocused(false)
	if p.focused {
		t.Error("SetFocused(false) did not clear p.focused")
	}
}

// ── View rendering ────────────────────────────────────────────────────────────

func TestWorkersPanel_View_empty(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetSize(80, 20)
	v := p.View()
	if v == "" {
		t.Error("View() returned empty string")
	}
	if !containsAny(v, "no active workers", "(no active") {
		t.Errorf("View() should indicate no workers, got: %q", v)
	}
}

func TestWorkersPanel_View_withWorkers(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetSize(80, 20)
	p.workers = []WorkerEntry{
		{Key: "exo-cli-swe-1", Project: "exo-cli", Tempo: "awaiting", Step: ""},
		{Key: "exo-cli-review-128", Project: "exo-cli", Tempo: "active", Step: "review-code"},
	}
	p.rebuildTable()
	v := p.View()
	if !containsAny(v, "exo-cli-swe-1", "exo-cli-review-128") {
		t.Errorf("View() should show worker names, got: %q", v)
	}
}

// ── Filtering ─────────────────────────────────────────────────────────────────

func TestWorkersPanel_filteredRows_noFilter(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.workers = []WorkerEntry{
		{Key: "worker-a", Project: "proj"},
		{Key: "worker-b", Project: "proj"},
	}
	rows := p.filteredRows()
	if len(rows) != 2 {
		t.Errorf("filteredRows() with no filter = %d rows, want 2", len(rows))
	}
}

func TestWorkersPanel_filteredRows_withFilter(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.workers = []WorkerEntry{
		{Key: "exo-swe-1", Project: "exo"},
		{Key: "charon-review-5", Project: "charon"},
		{Key: "exo-swe-2", Project: "exo"},
	}
	p.filter = "exo"
	rows := p.filteredRows()
	if len(rows) != 2 {
		t.Errorf("filteredRows(filter=%q) = %d rows, want 2", p.filter, len(rows))
	}
}

func TestWorkersPanel_filteredRows_caseInsensitive(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.workers = []WorkerEntry{
		{Key: "EXO-swe-1", Project: "exo"},
	}
	p.filter = "exo"
	rows := p.filteredRows()
	if len(rows) != 1 {
		t.Errorf("filteredRows() should be case-insensitive, got %d rows", len(rows))
	}
}

func TestWorkersPanel_View_filterNoMatch(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.workers = []WorkerEntry{{Key: "charon-swe", Project: "charon"}}
	p.filter = "zzz"
	p.rebuildTable()
	p.SetSize(80, 20)
	v := p.View()
	if !containsAny(v, "no vendangeurs match", "no vendangeurs match filter") {
		t.Errorf("View() with no-match filter should say so, got: %q", v)
	}
}

// ── Keyboard interactions ─────────────────────────────────────────────────────

func TestWorkersPanel_KeySlash_opensFilter(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetFocused(true)
	p.SetSize(80, 20)

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'/'}})
	wp := updated.(*WorkersPanel)
	if !wp.filtering {
		t.Error("pressing '/' should open the filter")
	}
}

func TestWorkersPanel_KeyEsc_closesFilter(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	p.SetFocused(true)
	p.filtering = true
	p.filterBox.Focus()

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyEsc})
	wp := updated.(*WorkersPanel)
	if wp.filtering {
		t.Error("pressing Esc should close the filter")
	}
}

func TestWorkersPanel_kvUpdateMsg(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	workers := []WorkerEntry{
		{Key: "exo-swe-1", Project: "exo", Tempo: "active", Step: "implement"},
	}
	updated, _ := p.Update(kvUpdateMsg{workers: workers})
	wp := updated.(*WorkersPanel)
	if len(wp.workers) != 1 {
		t.Errorf("after kvUpdateMsg, got %d workers, want 1", len(wp.workers))
	}
	if wp.workers[0].Key != "exo-swe-1" {
		t.Errorf("worker key = %q, want %q", wp.workers[0].Key, "exo-swe-1")
	}
}

func TestWorkersPanel_kvErrorMsg(t *testing.T) {
	p := NewWorkersPanel("vig", nil)
	import_err := fmt.Errorf("nats timeout") //nolint
	updated, _ := p.Update(kvErrorMsg{err: import_err})
	wp := updated.(*WorkersPanel)
	if wp.lastErr == nil {
		t.Error("kvErrorMsg should set lastErr")
	}
}

// ── KV parsing ────────────────────────────────────────────────────────────────

func TestParseKVEntry_validJSON(t *testing.T) {
	entry := &fakeKVEntry{
		key:   "exo-swe-1",
		value: []byte(`{"project":"exo","tempo":"active","step":"implement","vignoble":"prod"}`),
		op:    nats.KeyValuePut,
	}
	we, ok := parseKVEntry(entry)
	if !ok {
		t.Fatal("parseKVEntry returned ok=false for valid entry")
	}
	if we.Project != "exo" {
		t.Errorf("Project = %q, want %q", we.Project, "exo")
	}
	if we.Tempo != "active" {
		t.Errorf("Tempo = %q, want %q", we.Tempo, "active")
	}
	if we.Step != "implement" {
		t.Errorf("Step = %q, want %q", we.Step, "implement")
	}
	if we.vignoble != "prod" {
		t.Errorf("vignoble = %q, want %q", we.vignoble, "prod")
	}
}

func TestParseKVEntry_invalidJSON(t *testing.T) {
	entry := &fakeKVEntry{key: "k", value: []byte(`{bad`), op: nats.KeyValuePut}
	_, ok := parseKVEntry(entry)
	if ok {
		t.Error("parseKVEntry should return ok=false for invalid JSON")
	}
}

func TestParseKVEntry_nilValue(t *testing.T) {
	entry := &fakeKVEntry{key: "k", value: nil, op: nats.KeyValuePut}
	_, ok := parseKVEntry(entry)
	if ok {
		t.Error("parseKVEntry should return ok=false for nil value")
	}
}

func TestParseKVEntry_stepFromProcess(t *testing.T) {
	entry := &fakeKVEntry{
		key:   "k",
		value: []byte(`{"process":"swe","vignoble":"v"}`),
		op:    nats.KeyValuePut,
	}
	we, ok := parseKVEntry(entry)
	if !ok {
		t.Fatal("parseKVEntry returned ok=false")
	}
	if we.Step != "swe" {
		t.Errorf("Step should default to process name, got %q", we.Step)
	}
}

// ── sortedWorkers ─────────────────────────────────────────────────────────────

func TestSortedWorkers(t *testing.T) {
	m := map[string]WorkerEntry{
		"zzz": {Key: "zzz"},
		"aaa": {Key: "aaa"},
		"mmm": {Key: "mmm"},
	}
	result := sortedWorkers(m)
	if len(result) != 3 {
		t.Fatalf("want 3 workers, got %d", len(result))
	}
	if result[0].Key != "aaa" || result[1].Key != "mmm" || result[2].Key != "zzz" {
		t.Errorf("sortedWorkers order wrong: got %v", result)
	}
}

// ── max helper ───────────────────────────────────────────────────────────────

func TestMax(t *testing.T) {
	if max(3, 5) != 5 {
		t.Error("max(3,5) should be 5")
	}
	if max(10, 2) != 10 {
		t.Error("max(10,2) should be 10")
	}
	if max(7, 7) != 7 {
		t.Error("max(7,7) should be 7")
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func containsAny(s string, subs ...string) bool {
	for _, sub := range subs {
		if len(sub) > 0 {
			for i := 0; i <= len(s)-len(sub); i++ {
				if s[i:i+len(sub)] == sub {
					return true
				}
			}
		}
	}
	return false
}

// fakeKVEntry implements nats.KeyValueEntry for testing.
type fakeKVEntry struct {
	key   string
	value []byte
	op    nats.KeyValueOp
}

func (f *fakeKVEntry) Bucket() string                  { return "pinard-agents" }
func (f *fakeKVEntry) Key() string                     { return f.key }
func (f *fakeKVEntry) Value() []byte                   { return f.value }
func (f *fakeKVEntry) Revision() uint64                { return 1 }
func (f *fakeKVEntry) Delta() uint64                   { return 0 }
func (f *fakeKVEntry) Created() time.Time { return time.Time{} }
func (f *fakeKVEntry) Operation() nats.KeyValueOp      { return f.op }
