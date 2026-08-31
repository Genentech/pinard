// Package webterm implements Phase 1 of web-terminal-access: a signed-link,
// read-only browser view of a single tmux session, bridged from a k8s gateway to
// the tmux host over NATS (never JetStream — terminal bytes are ephemeral).
//
// Transport (core NATS):
//
//	browser ⇄ WebSocket ⇄ gateway ⇄ core NATS ⇄ responder ⇄ tmux attach -r
//
// The responder subscribes to a per-vignoble request subject; the gateway issues
// a request carrying a short-lived HMAC grant and a unique per-viewer ID. Once a
// viewer is accepted, a set of per-viewer subjects carries the live stream.
package webterm

import "fmt"

// ReqSubject is where responders listen for viewer requests (request/reply).
func ReqSubject(vignoble string) string {
	return fmt.Sprintf("pinard.%s.webterm.req", vignoble)
}

// ListSubject is where responders answer control-room enumeration requests
// (request/reply): the live tmux sessions + conductor windows for a vignoble.
func ListSubject(vignoble string) string {
	return fmt.Sprintf("pinard.%s.webterm.list", vignoble)
}

// ListReq is the enumeration request published by the gateway to ListSubject.
type ListReq struct {
	Grant string `json:"grant"` // gateway grant (vignoble-scoped, Mode=ModeList)
}

// WinInfo is a conductor window (régisseur = index 0; maîtres named per parcelle).
type WinInfo struct {
	Index int    `json:"index"`
	Name  string `json:"name"`
}

// ListReply is the responder's enumeration reply.
type ListReply struct {
	OK       bool      `json:"ok"`
	Sessions []string  `json:"sessions"` // all tmux session names (incl. conductor)
	Windows  []WinInfo `json:"windows"`  // conductor windows (régisseur + maîtres)
}

// viewerBase is the per-viewer subject prefix.
func viewerBase(vignoble, viewerID string) string {
	return fmt.Sprintf("pinard.%s.webterm.%s", vignoble, viewerID)
}

// OutSubject: responder → gateway, raw PTY bytes (binary).
func OutSubject(vignoble, viewerID string) string { return viewerBase(vignoble, viewerID) + ".out" }

// InSubject: gateway → responder, raw input bytes (unused in read-only Phase 1).
func InSubject(vignoble, viewerID string) string { return viewerBase(vignoble, viewerID) + ".in" }

// CtlSubject: gateway → responder, JSON control (resize/close/heartbeat).
func CtlSubject(vignoble, viewerID string) string { return viewerBase(vignoble, viewerID) + ".ctl" }

// EvtSubject: responder → gateway, JSON events (ended).
func EvtSubject(vignoble, viewerID string) string { return viewerBase(vignoble, viewerID) + ".evt" }

// ReqMsg is the viewer-open request published by the gateway to ReqSubject.
type ReqMsg struct {
	Grant    string `json:"grant"`     // signed gateway grant (target+mode+exp)
	ViewerID string `json:"viewer_id"` // unique per open; scopes the per-viewer subjects
	Cols     int    `json:"cols"`
	Rows     int    `json:"rows"`
}

// ReqReply is the responder's reply to a ReqMsg.
type ReqReply struct {
	OK     bool   `json:"ok"`
	Reason string `json:"reason,omitempty"`
}

// Control message types (gateway → responder, on CtlSubject).
const (
	CtlResize    = "resize"
	CtlClose     = "close"
	CtlHeartbeat = "heartbeat"
	CtlInterrupt = "interrupt"
)

// CtlMsg is a gateway → responder control message.
type CtlMsg struct {
	Type string `json:"type"`
	Cols int    `json:"cols,omitempty"`
	Rows int    `json:"rows,omitempty"`
}

// Event types (responder → gateway, on EvtSubject).
const EvtEnded = "ended"

// EvtMsg is a responder → gateway event message.
type EvtMsg struct {
	Type   string `json:"type"`
	Reason string `json:"reason,omitempty"`
}

// Grant modes.
const (
	ModeRO   = "ro"
	ModeRW   = "rw"
	ModeList = "list" // control-room enumeration grant
)

// PtyOutSubject is the stable per-agent subject on which a local PTY pump
// publishes raw terminal output. It does not require a grant — NATS auth is
// the trust boundary for this read-only channel.
//
// Format: pinard.<vignoble>.agents.<agentID>.pty.out
func PtyOutSubject(vignoble, agentID string) string {
	return fmt.Sprintf("pinard.%s.agents.%s.pty.out", vignoble, agentID)
}
