package webterm

import "testing"

func TestSubjectFormats(t *testing.T) {
	if got := ReqSubject("huge"); got != "pinard.huge.webterm.req" {
		t.Errorf("ReqSubject = %q", got)
	}
	if got := OutSubject("huge", "v1"); got != "pinard.huge.webterm.v1.out" {
		t.Errorf("OutSubject = %q", got)
	}
	if got := InSubject("huge", "v1"); got != "pinard.huge.webterm.v1.in" {
		t.Errorf("InSubject = %q", got)
	}
	if got := CtlSubject("huge", "v1"); got != "pinard.huge.webterm.v1.ctl" {
		t.Errorf("CtlSubject = %q", got)
	}
	if got := EvtSubject("huge", "v1"); got != "pinard.huge.webterm.v1.evt" {
		t.Errorf("EvtSubject = %q", got)
	}

}
