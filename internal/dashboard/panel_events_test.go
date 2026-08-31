package dashboard

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/nats-io/nats.go"
)

// ── fakeJS ───────────────────────────────────────────────────────────────────

// fakeJS satisfies the jsSubscriber interface without a real NATS connection.
type fakeJS struct {
	// subscriptions accumulated by test code.
	subs []fakeSubscribeCall
}

type fakeSubscribeCall struct {
	subject string
	handler nats.MsgHandler
}

func (f *fakeJS) Subscribe(subj string, cb nats.MsgHandler, opts ...nats.SubOpt) (*nats.Subscription, error) {
	f.subs = append(f.subs, fakeSubscribeCall{subject: subj, handler: cb})
	return nil, nil
}

// ── helpers ──────────────────────────────────────────────────────────────────

func newTestEventsPanel() *EventsPanel {
	p := NewEventsPanel(nil) // nil = offline mode
	p.SetSize(80, 20)
	return p
}

func newOnlineTestPanel() *EventsPanel {
	p := NewEventsPanel(&fakeJS{})
	p.SetSize(80, 20)
	return p
}

func makeEntry(eventType, subject string, minutesAgo int) EventEntry {
	return EventEntry{
		At:        time.Now().Add(-time.Duration(minutesAgo) * time.Minute),
		EventType: eventType,
		Subject:   subject,
		Raw:       fmt.Sprintf("pinard.v1.agents.%s.events.%s", subject, eventType),
	}
}

// ── parseEventMsg ─────────────────────────────────────────────────────────────

func TestParseEventMsg_subjectParsing(t *testing.T) {
	cases := []struct {
		subject  string
		wantType string
		wantSess string
	}{
		{
			"pinard.vignoble1.parcelles.exo-cli.agents.exo-cli-swe-128.events.pipeline_passed",
			"pipeline_passed",
			"exo-cli-swe-128",
		},
		{
			"pinard.prod.parcelles.charon.agents.charon-review-10.events.review_comment",
			"review_comment",
			"charon-review-10",
		},
		{
			"short",
			"",
			"",
		},
	}

	for _, tc := range cases {
		t.Run(tc.subject, func(t *testing.T) {
			msg := &nats.Msg{Subject: tc.subject, Data: []byte(`{}`)}
			entry := parseEventMsg(msg)
			if entry.EventType != tc.wantType {
				t.Errorf("EventType: got %q, want %q", entry.EventType, tc.wantType)
			}
			if tc.wantSess != "" && entry.Subject != tc.wantSess {
				// When no project in payload, Subject falls back to session.
				t.Errorf("Subject: got %q, want %q", entry.Subject, tc.wantSess)
			}
		})
	}
}

func TestParseEventMsg_projectOverridesSession(t *testing.T) {
	payload, _ := json.Marshal(map[string]any{"project": "exo-cli", "mr": 128})
	msg := &nats.Msg{
		Subject: "pinard.v1.parcelles.exo-cli.agents.exo-cli-swe-128.events.pipeline_passed",
		Data:    payload,
	}
	entry := parseEventMsg(msg)
	if entry.Subject != "exo-cli" {
		t.Errorf("Subject: got %q, want exo-cli", entry.Subject)
	}
	if entry.EventType != "pipeline_passed" {
		t.Errorf("EventType: got %q, want pipeline_passed", entry.EventType)
	}
}

func TestParseEventMsg_repoFallback(t *testing.T) {
	payload, _ := json.Marshal(map[string]any{"repo": "group/my-repo"})
	msg := &nats.Msg{
		Subject: "pinard.v1.parcelles.group-my-repo.agents.worker-1.events.review_comment",
		Data:    payload,
	}
	entry := parseEventMsg(msg)
	if entry.Subject != "group/my-repo" {
		t.Errorf("Subject: got %q, want group/my-repo", entry.Subject)
	}
}

// ── rolling buffer ────────────────────────────────────────────────────────────

func TestAddEvent_cappedAtMax(t *testing.T) {
	p := newTestEventsPanel()
	for i := 0; i < maxEvents+10; i++ {
		p.addEvent(makeEntry("pipeline_passed", fmt.Sprintf("proj-%d", i), 0))
	}
	if len(p.events) != maxEvents {
		t.Errorf("events len: got %d, want %d", len(p.events), maxEvents)
	}
}

func TestAddEvent_newestFirst(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("pipeline_passed", "first", 5))
	p.addEvent(makeEntry("review_comment", "second", 1))

	if p.events[0].EventType != "review_comment" {
		t.Errorf("expected newest first, got %q", p.events[0].EventType)
	}
	if p.events[1].EventType != "pipeline_passed" {
		t.Errorf("expected older second, got %q", p.events[1].EventType)
	}
}

// ── filter ───────────────────────────────────────────────────────────────────

func TestFilter_byEventType(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 5))
	p.addEvent(makeEntry("pipeline_failed", "exo-cli", 4))
	p.addEvent(makeEntry("review_comment", "exo-cli", 3))

	p.filter = "failed"
	p.applyFilter()

	if len(p.filtered) != 1 {
		t.Fatalf("expected 1 filtered result, got %d", len(p.filtered))
	}
	if p.filtered[0].EventType != "pipeline_failed" {
		t.Errorf("expected pipeline_failed, got %q", p.filtered[0].EventType)
	}
}

func TestFilter_bySubject(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 5))
	p.addEvent(makeEntry("pipeline_passed", "charon", 4))

	p.filter = "charon"
	p.applyFilter()

	if len(p.filtered) != 1 {
		t.Fatalf("expected 1 filtered result, got %d", len(p.filtered))
	}
	if p.filtered[0].Subject != "charon" {
		t.Errorf("expected charon, got %q", p.filtered[0].Subject)
	}
}

func TestFilter_emptyShowsAll(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 5))
	p.addEvent(makeEntry("review_comment", "charon", 3))

	p.filter = ""
	p.applyFilter()

	if len(p.filtered) != 2 {
		t.Errorf("expected 2 results with empty filter, got %d", len(p.filtered))
	}
}

func TestFilter_caseInsensitive(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("PIPELINE_PASSED", "ExoCli", 1))

	p.filter = "pipeline"
	p.applyFilter()

	if len(p.filtered) != 1 {
		t.Errorf("expected 1 result for case-insensitive filter, got %d", len(p.filtered))
	}
}

func TestFilter_noMatchShowsEmpty(t *testing.T) {
	p := newTestEventsPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 1))

	p.filter = "zzznomatch"
	p.applyFilter()

	if len(p.filtered) != 0 {
		t.Errorf("expected 0 results, got %d", len(p.filtered))
	}
}

// ── View ─────────────────────────────────────────────────────────────────────

func TestView_offlineMode(t *testing.T) {
	p := newTestEventsPanel() // js == nil
	view := p.View()
	if !strings.Contains(view, "NATS unavailable") {
		t.Errorf("expected 'NATS unavailable' in view, got:\n%s", view)
	}
}

func TestView_noEvents(t *testing.T) {
	p := newOnlineTestPanel()
	view := p.View()
	if !strings.Contains(view, "no events yet") {
		t.Errorf("expected 'no events yet' in view, got:\n%s", view)
	}
}

func TestView_noMatchFilter(t *testing.T) {
	p := newOnlineTestPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 1))
	p.filter = "zzznomatch"
	p.applyFilter()
	p.rebuildViewport()

	view := p.View()
	if !strings.Contains(view, "no events match filter") {
		t.Errorf("expected 'no events match filter' in view, got:\n%s", view)
	}
}

func TestView_rendersEventLines(t *testing.T) {
	p := newOnlineTestPanel()
	p.addEvent(EventEntry{
		At:        time.Date(2026, 6, 5, 14, 15, 0, 0, time.UTC),
		EventType: "pipeline_passed",
		Subject:   "exo-cli",
		Raw:       "pinard.v1.agents.exo-cli-swe-128.events.pipeline_passed",
	})
	p.rebuildViewport()

	view := p.View()
	if !strings.Contains(view, "pipeline_passed") {
		t.Errorf("expected 'pipeline_passed' in view, got:\n%s", view)
	}
	if !strings.Contains(view, "exo-cli") {
		t.Errorf("expected 'exo-cli' in view, got:\n%s", view)
	}
	if !strings.Contains(view, "14:15") {
		t.Errorf("expected '14:15' timestamp in view, got:\n%s", view)
	}
}

func TestView_showsFilterLabel(t *testing.T) {
	p := newOnlineTestPanel()
	p.addEvent(makeEntry("pipeline_passed", "exo-cli", 1))
	p.filter = "pipeline"
	p.applyFilter()
	p.rebuildViewport()

	view := p.View()
	if !strings.Contains(view, "filter: pipeline") {
		t.Errorf("expected filter label in view, got:\n%s", view)
	}
}

// ── Panel interface ───────────────────────────────────────────────────────────

func TestEventsPanel_Title(t *testing.T) {
	p := NewEventsPanel(nil)
	if p.Title() != "Events" {
		t.Errorf("expected title 'Events', got %q", p.Title())
	}
}

func TestEventsPanel_SetFocused(t *testing.T) {
	p := NewEventsPanel(nil)
	p.SetFocused(true)
	if !p.focused {
		t.Error("expected focused=true after SetFocused(true)")
	}
	p.SetFocused(false)
	if p.focused {
		t.Error("expected focused=false after SetFocused(false)")
	}
}

func TestEventsPanel_ImplementsPanel(t *testing.T) {
	var _ Panel = NewEventsPanel(nil)
}

// ── Scrolling ────────────────────────────────────────────────────────────────

func TestScrolling_jDisablesAutoScroll(t *testing.T) {
	p := newOnlineTestPanel()
	p.focused = true
	for i := 0; i < 5; i++ {
		p.addEvent(makeEntry("pipeline_passed", fmt.Sprintf("proj-%d", i), i))
	}
	p.rebuildViewport()

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("j")})
	ep := updated.(*EventsPanel)
	if ep.autoScroll {
		t.Error("autoScroll should be false after pressing j")
	}
}

func TestScrolling_kDisablesAutoScroll(t *testing.T) {
	p := newOnlineTestPanel()
	p.focused = true
	for i := 0; i < 5; i++ {
		p.addEvent(makeEntry("pipeline_passed", fmt.Sprintf("proj-%d", i), i))
	}
	p.rebuildViewport()

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("k")})
	ep := updated.(*EventsPanel)
	if ep.autoScroll {
		t.Error("autoScroll should be false after pressing k")
	}
}

func TestScrolling_gRestoresAutoScroll(t *testing.T) {
	p := newOnlineTestPanel()
	p.focused = true
	p.autoScroll = false

	updated, _ := p.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("g")})
	ep := updated.(*EventsPanel)
	if !ep.autoScroll {
		t.Error("autoScroll should be true after pressing g")
	}
}

// ── eventMsg handling ─────────────────────────────────────────────────────────

func TestUpdate_eventMsgAddsEntry(t *testing.T) {
	p := newOnlineTestPanel()
	entry := makeEntry("needs_approval", "exo-cli", 0)

	updated, _ := p.Update(eventMsg{entry: entry})
	ep := updated.(*EventsPanel)
	if len(ep.events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(ep.events))
	}
	if ep.events[0].EventType != "needs_approval" {
		t.Errorf("unexpected event type %q", ep.events[0].EventType)
	}
}

// ── Init offline ─────────────────────────────────────────────────────────────

func TestInit_nilJsReturnsNilCmd(t *testing.T) {
	p := NewEventsPanel(nil)
	cmd := p.Init()
	if cmd != nil {
		t.Error("expected nil Cmd when js is nil")
	}
}
