package pnats

import "testing"

func TestAgentEventsSubject(t *testing.T) {
	if got := AgentEventsSubject("exohub", "exo-cli", "w1", "", "agent_idle"); got != "pinard.exohub.parcelles.exo-cli.agents.w1.events.agent_idle" {
		t.Errorf("plain events subject wrong: %q", got)
	}
	if got := AgentEventsSubject("exohub", "semantic-search", "run-1", "dev", "session_ended"); got != "pinard.exohub.parcelles.semantic-search.agents.run-1.process.dev.events.session_ended" {
		t.Errorf("process events subject wrong: %q", got)
	}
}

func TestWorkerInboxSubject(t *testing.T) {
	if got := WorkerInboxSubject("exohub", "exo-cli", "w1", ""); got != "pinard.exohub.parcelles.exo-cli.agents.w1.inbox" {
		t.Errorf("plain inbox wrong: %q", got)
	}
	if got := WorkerInboxSubject("exohub", "infra", "run-2", "swe"); got != "pinard.exohub.parcelles.infra.agents.run-2.process.swe.inbox" {
		t.Errorf("process inbox wrong: %q", got)
	}
}

func TestParseAgentSubject(t *testing.T) {
	cases := []struct {
		subject                        string
		wantParcelle, wantAgent, wantT string
		wantOK                         bool
	}{
		{"pinard.exohub.parcelles.exo-cli.agents.w1.events.agent_idle", "exo-cli", "w1", "agent_idle", true},
		{"pinard.exohub.parcelles.semantic-search.agents.run-1.process.dev.events.session_ended", "semantic-search", "run-1", "session_ended", true},
		{"pinard.exohub.issues.new", "", "", "", false},
		{"garbage", "", "", "", false},
	}
	for _, c := range cases {
		p, a, et, ok := ParseAgentSubject(c.subject)
		if ok != c.wantOK || p != c.wantParcelle || a != c.wantAgent || et != c.wantT {
			t.Errorf("ParseAgentSubject(%q) = (%q,%q,%q,%v), want (%q,%q,%q,%v)",
				c.subject, p, a, et, ok, c.wantParcelle, c.wantAgent, c.wantT, c.wantOK)
		}
	}
}

// Round-trip: building then parsing yields the original components.
func TestSubjectRoundTrip(t *testing.T) {
	subj := AgentEventsSubject("v", "my-parcelle", "agent-9", "", "pipeline_failed")
	p, a, et, ok := ParseAgentSubject(subj)
	if !ok || p != "my-parcelle" || a != "agent-9" || et != "pipeline_failed" {
		t.Errorf("round-trip failed: %q -> (%q,%q,%q,%v)", subj, p, a, et, ok)
	}
}
