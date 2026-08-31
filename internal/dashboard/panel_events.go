package dashboard

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/nats-io/nats.go"

	"github.com/Genentech/pinard/internal/pnats"
)

const maxEvents = 50

// EventEntry holds a single received agent event.
type EventEntry struct {
	At        time.Time
	EventType string // pipeline_passed, pipeline_failed, review_comment, …
	Subject   string // project/session/MR identifier extracted from NATS subject
	Raw       string // original NATS subject for display/filter
}

// eventMsg is the Bubble Tea message delivered when a new event arrives.
type eventMsg struct{ entry EventEntry }

// eventsErrMsg is sent when the JetStream subscription fails.
type eventsErrMsg struct{ err error }

// jsSubscriber is the minimal JetStream interface the EventsPanel needs.
// It is satisfied by nats.JetStreamContext and can be faked in tests.
type jsSubscriber interface {
	Subscribe(subj string, cb nats.MsgHandler, opts ...nats.SubOpt) (*nats.Subscription, error)
}

// EventsPanel is the Panel implementation for the Events feed.
type EventsPanel struct {
	js jsSubscriber

	events   []EventEntry // newest first, capped at maxEvents
	filter   string
	filtered []EventEntry // cached filtered slice

	viewport  viewport.Model
	filterBox textinput.Model

	filtering  bool
	autoScroll bool // true when the user has not manually scrolled up
	focused    bool
	width      int
	height     int

	evCh    chan EventEntry // live event channel; set after subscription
	lastErr error
}

// NewEventsPanel creates an Events panel. js may be nil (offline mode).
func NewEventsPanel(js jsSubscriber) *EventsPanel {
	fi := textinput.New()
	fi.Placeholder = "filter events…"
	fi.CharLimit = 60

	vp := viewport.New(40, 6)

	p := &EventsPanel{
		js:         js,
		viewport:   vp,
		filterBox:  fi,
		autoScroll: true,
	}
	return p
}

// ── Panel interface ──────────────────────────────────────────────────────────

func (p *EventsPanel) Title() string { return "Events" }

func (p *EventsPanel) Init() tea.Cmd {
	if p.js == nil || p.evCh != nil {
		return nil
	}
	return p.subscribe()
}

func (p *EventsPanel) Update(msg tea.Msg) (Panel, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case eventMsg:
		p.lastErr = nil
		p.addEvent(msg.entry)
		p.rebuildViewport()
		// Continue listening for the next event.
		cmds = append(cmds, p.waitForEvent())

	case eventsErrMsg:
		p.lastErr = msg.err

	case tea.KeyMsg:
		if p.filtering {
			switch msg.String() {
			case "esc", "enter":
				p.filtering = false
				p.filterBox.Blur()
			default:
				var cmd tea.Cmd
				p.filterBox, cmd = p.filterBox.Update(msg)
				cmds = append(cmds, cmd)
				p.filter = p.filterBox.Value()
				p.applyFilter()
				p.rebuildViewport()
			}
			return p, tea.Batch(cmds...)
		}

		if !p.focused {
			break
		}
		switch msg.String() {
		case "j", "down":
			p.autoScroll = false
			p.viewport.LineDown(1)
		case "k", "up":
			p.autoScroll = false
			p.viewport.LineUp(1)
		case "g":
			p.autoScroll = true
			p.viewport.GotoTop()
		case "G":
			p.autoScroll = false
			p.viewport.GotoBottom()
		case "/":
			p.filtering = true
			p.filterBox.SetValue("")
			p.filter = ""
			p.filterBox.Focus()
			p.applyFilter()
			p.rebuildViewport()
		case "esc":
			if p.filter != "" {
				p.filter = ""
				p.filterBox.SetValue("")
				p.applyFilter()
				p.rebuildViewport()
			}
		}
	}

	var vpCmd tea.Cmd
	p.viewport, vpCmd = p.viewport.Update(msg)
	if vpCmd != nil {
		cmds = append(cmds, vpCmd)
	}

	return p, tea.Batch(cmds...)
}

func (p *EventsPanel) View() string {
	innerW := p.width - 4
	if innerW < 1 {
		innerW = 1
	}

	var sb strings.Builder

	if p.filtering {
		sb.WriteString(p.filterBox.View())
		sb.WriteString("\n")
	} else if p.filter != "" {
		filterStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
		sb.WriteString(filterStyle.Render(fmt.Sprintf("filter: %s", p.filter)))
		sb.WriteString("\n")
	}

	if p.js == nil {
		content := sb.String() + dimStyle.Render("(NATS unavailable)")
		return renderPanel(p.Title(), content, p.focused, p.width, p.height)
	}

	if p.lastErr != nil {
		errStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("196"))
		content := sb.String() + errStyle.Render(fmt.Sprintf("⚠ %v", p.lastErr))
		return renderPanel(p.Title(), content, p.focused, p.width, p.height)
	}

	if len(p.filtered) == 0 {
		var msg string
		if p.filter != "" {
			msg = "(no events match filter)"
		} else {
			msg = "(no events yet)"
		}
		content := sb.String() + dimStyle.Render(msg)
		return renderPanel(p.Title(), content, p.focused, p.width, p.height)
	}

	sb.WriteString(p.viewport.View())
	return renderPanel(p.Title(), sb.String(), p.focused, p.width, p.height)
}

func (p *EventsPanel) SetSize(width, height int) {
	p.width = width
	p.height = height

	innerW := width - 4
	if innerW < 1 {
		innerW = 1
	}

	vpH := height - 4 // border(2) + title(1) + filter row(1)
	if p.filtering || p.filter != "" {
		vpH--
	}
	if vpH < 1 {
		vpH = 1
	}

	p.viewport.Width = innerW
	p.viewport.Height = vpH
	p.filterBox.Width = innerW
	p.rebuildViewport()
}

func (p *EventsPanel) SetFocused(focused bool) {
	p.focused = focused
}

// ── Internal helpers ─────────────────────────────────────────────────────────

func (p *EventsPanel) addEvent(e EventEntry) {
	// Prepend (newest first)
	p.events = append([]EventEntry{e}, p.events...)
	if len(p.events) > maxEvents {
		p.events = p.events[:maxEvents]
	}
	p.applyFilter()
}

func (p *EventsPanel) applyFilter() {
	if p.filter == "" {
		p.filtered = p.events
		return
	}
	lower := strings.ToLower(p.filter)
	out := make([]EventEntry, 0, len(p.events))
	for _, e := range p.events {
		if strings.Contains(strings.ToLower(e.EventType), lower) ||
			strings.Contains(strings.ToLower(e.Subject), lower) ||
			strings.Contains(strings.ToLower(e.Raw), lower) {
			out = append(out, e)
		}
	}
	p.filtered = out
}

func (p *EventsPanel) rebuildViewport() {
	innerW := p.width - 4
	if innerW < 1 {
		innerW = 1
	}

	var sb strings.Builder
	for _, e := range p.filtered {
		ts := e.At.Format("15:04")
		// Truncate subject to available width
		subject := e.Subject
		typeW := 24
		subjectW := innerW - 6 - typeW - 3
		if subjectW < 1 {
			subjectW = 1
		}
		if len(subject) > subjectW {
			subject = subject[:subjectW]
		}
		eventTypeStr := e.EventType
		if len(eventTypeStr) > typeW {
			eventTypeStr = eventTypeStr[:typeW]
		}

		line := fmt.Sprintf("%s %-*s %s", ts, typeW, eventTypeStr, subject)
		sb.WriteString(line)
		sb.WriteString("\n")
	}

	p.viewport.SetContent(sb.String())
	if p.autoScroll {
		p.viewport.GotoTop()
	}
}

// ── JetStream subscription ───────────────────────────────────────────────────

func (p *EventsPanel) subscribe() tea.Cmd {
	return func() tea.Msg {
		ch := make(chan EventEntry, 64)

		// Use an ephemeral push consumer (no durable name) so we get live events.
		// DeliverLastPolicy delivers the most recent message first, then live.
		_, err := p.js.Subscribe(pnats.StreamSubjectAgentEvents, func(m *nats.Msg) {
			entry := parseEventMsg(m)
			ch <- entry
			_ = m.Ack()
		}, nats.DeliverLast(), nats.AckExplicit())
		if err != nil {
			return eventsErrMsg{err: fmt.Errorf("subscribe events: %w", err)}
		}
		// Subscription is kept alive by the NATS runtime (message handler goroutine).
		// Store the channel so subsequent waitForEvent calls can drain it.
		p.evCh = ch

		// Block until the first event arrives.
		e := <-ch
		return eventMsg{entry: e}
	}
}

// waitForEvent blocks until the next event arrives on the shared channel.
func (p *EventsPanel) waitForEvent() tea.Cmd {
	ch := p.evCh
	if ch == nil {
		return nil
	}
	return func() tea.Msg {
		e := <-ch
		return eventMsg{entry: e}
	}
}

// parseEventMsg extracts an EventEntry from a NATS message.
// Subject format: pinard.{vignoble}.parcelles.{parcelle}.agents.{session}.events.{eventType}
func parseEventMsg(m *nats.Msg) EventEntry {
	_, session, eventType, _ := pnats.ParseAgentSubject(m.Subject)

	// Try to extract project from the JSON payload.
	project := ""
	if m.Data != nil {
		var data map[string]any
		if err := json.Unmarshal(m.Data, &data); err == nil {
			project, _ = data["project"].(string)
			if project == "" {
				project, _ = data["repo"].(string)
			}
		}
	}

	subject := session
	if project != "" {
		subject = project
	}

	return EventEntry{
		At:        time.Now(),
		EventType: eventType,
		Subject:   subject,
		Raw:       m.Subject,
	}
}
