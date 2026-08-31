package pnats

import (
	"fmt"
	"strings"
)

// Agent-scoped subject hierarchy:
//
//	pinard.<vignoble>.parcelles.<parcelle>.agents.<agentID>[.process.<proc>].{events.<type>|inbox|btw|interrupt}
//
// The literal `parcelles` segment namespaces per-parcelle traffic so a parcelle
// named like a fixed top-level token (issues/schedules/notifications) cannot
// collide. Agent events always carry a parcelle (a worker is spawned with one,
// defaulting to the project). Vignoble-level subjects (issues/schedules/
// notifications/dashboard) are NOT parcelle-scoped and are built elsewhere.

func agentBase(vignoble, parcelle, agentID string) string {
	return fmt.Sprintf("pinard.%s.parcelles.%s.agents.%s", vignoble, parcelle, agentID)
}

// AgentEventsSubject builds the events subject for a worker/agent.
func AgentEventsSubject(vignoble, parcelle, agentID, processName, eventType string) string {
	if processName != "" {
		return fmt.Sprintf("%s.process.%s.events.%s", agentBase(vignoble, parcelle, agentID), processName, eventType)
	}
	return fmt.Sprintf("%s.events.%s", agentBase(vignoble, parcelle, agentID), eventType)
}

// WorkerInboxSubject builds the inbox subject (conductor/maître → worker).
func WorkerInboxSubject(vignoble, parcelle, agentID, processName string) string {
	if processName != "" {
		return fmt.Sprintf("%s.process.%s.inbox", agentBase(vignoble, parcelle, agentID), processName)
	}
	return fmt.Sprintf("%s.inbox", agentBase(vignoble, parcelle, agentID))
}

// AgentBtwSubject builds the BTW (parallel-question) subject.
func AgentBtwSubject(vignoble, parcelle, session string) string {
	return fmt.Sprintf("%s.btw", agentBase(vignoble, parcelle, session))
}

// NotificationsSubject builds the worker→conductor notification subject. With a
// parcelle it is parcelle-scoped (delivered to that parcelle's maître);
// without, it is the vignoble-level channel (delivered to the régisseur).
func NotificationsSubject(vignoble, parcelle string) string {
	if parcelle != "" {
		return fmt.Sprintf("pinard.%s.parcelles.%s.notifications", vignoble, parcelle)
	}
	return fmt.Sprintf("pinard.%s.notifications", vignoble)
}

// AgentInterruptSubject builds the interrupt (cancel-turn) subject.
func AgentInterruptSubject(vignoble, parcelle, session string) string {
	return fmt.Sprintf("%s.interrupt", agentBase(vignoble, parcelle, session))
}

// Stream subject wildcards. Agent traffic is captured per top-level kind; the
// `parcelles.*` wildcard lets a single stream span all parcelles while maître
// consumers filter to their own parcelle.
const (
	StreamSubjectAgentEvents = "pinard.*.parcelles.*.agents.*.events.>"
	StreamSubjectInboxes     = "pinard.*.parcelles.*.agents.*.inbox"
	StreamSubjectProcesses   = "pinard.*.parcelles.*.agents.*.process.>"
	// StreamSubjectMemory captures durable memory-layer traffic (episodes, dead-letter, etc.).
	// Durable consumers per vignoble filter to pinard.<v>.memory.>.
	// Ephemeral request-reply subjects (recall, query) are intentionally kept
	// outside this hierarchy (see RecallSubject / QuerySubject) so they are
	// never captured by the stream and do not receive spurious JetStream PubAcks.
	StreamSubjectMemory = "pinard.*.memory.>"
)

// MemorySubject builds a subject in the pinard-memory stream.
// suffix is the remainder after the vignoble, e.g. "episodes" or "teaching.mysession".
func MemorySubject(vignoble, suffix string) string {
	return fmt.Sprintf("pinard.%s.memory.%s", vignoble, suffix)
}

// RecallSubject builds the request-reply subject for mid-session recall.
// It lives outside pinard.*.memory.> so the pinard-memory stream never
// captures it and JetStream PubAcks cannot corrupt nc.request() calls.
func RecallSubject(vignoble string) string {
	return fmt.Sprintf("pinard.%s.recall", vignoble)
}

// QuerySubject builds the request-reply subject for boot-recipe queries.
// It lives outside pinard.*.memory.> for the same reason as RecallSubject.
func QuerySubject(vignoble string) string {
	return fmt.Sprintf("pinard.%s.query", vignoble)
}

// BootRecallSubject builds the request-reply subject for boot-time hierarchical
// knowledge injection. Lives outside pinard.*.memory.> for the same reason as
// RecallSubject — never captured by the stream, no spurious JetStream PubAcks.
func BootRecallSubject(vignoble string) string {
	return fmt.Sprintf("pinard.%s.recall.boot", vignoble)
}

// ParseAgentSubject extracts (parcelle, agentID, eventType) from an agent-events
// subject. It is token-based (locates the `parcelles`/`agents`/`events` markers)
// so it is robust to optional `.process.<proc>` segments. eventType is the
// dot-joined remainder after `events`. ok is false if the subject is not an
// agent-events subject.
func ParseAgentSubject(subject string) (parcelle, agentID, eventType string, ok bool) {
	parts := strings.Split(subject, ".")
	idx := func(tok string) int {
		for i, p := range parts {
			if p == tok {
				return i
			}
		}
		return -1
	}
	pi, ai, ei := idx("parcelles"), idx("agents"), idx("events")
	if pi < 0 || ai < 0 || ei < 0 || pi+1 >= ai || ai+1 >= len(parts) || ei+1 >= len(parts) {
		return "", "", "", false
	}
	parcelle = parts[pi+1]
	agentID = parts[ai+1]
	eventType = strings.Join(parts[ei+1:], ".")
	return parcelle, agentID, eventType, true
}
