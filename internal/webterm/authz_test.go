package webterm

import "testing"

func TestAuthorizeMatrix(t *testing.T) {
	op := Identity{Username: "lelongs"}
	viewer := Identity{Username: "someone"}
	funder := Identity{Username: "funder1"}
	anon := Identity{}

	cases := []struct {
		name         string
		id           Identity
		linkValid    bool
		isOperator   bool
		isFunder     bool
		authRequired bool
		writable     bool
		wantAllowed  bool
		wantRole     string
		wantMode     string
	}{
		{"operator any target", op, false, true, false, true, false, true, "operator", ModeRO},
		{"operator writable steer", op, false, true, false, true, true, true, "operator", ModeRW},
		{"viewer with valid link", viewer, true, false, false, true, false, true, "viewer", ModeRO},
		{"viewer cannot go writable", viewer, true, false, false, true, true, true, "viewer", ModeRO},
		{"viewer without link denied", viewer, false, false, false, true, false, false, "", ""},
		{"unauth denied when auth required", anon, true, false, false, true, false, false, "", ""},
		{"auth-disabled link suffices", anon, true, false, false, false, false, true, "viewer", ModeRO},
		{"auth-disabled no link denied", anon, false, false, false, false, false, false, "", ""},
		// Funder cases.
		{"funder match gets RO", funder, false, false, true, true, false, true, "funder", ModeRO},
		{"funder requesting rw still gets RO", funder, false, false, true, true, true, true, "funder", ModeRO},
		{"non-funder authenticated denied", viewer, false, false, false, true, false, false, "", ""},
		{"unauthenticated funder-slot denied", anon, false, false, true, true, false, false, "", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			d := Authorize(c.id, c.linkValid, c.isOperator, c.isFunder, c.authRequired, c.writable)
			if d.Allowed != c.wantAllowed {
				t.Fatalf("allowed=%v want %v (%+v)", d.Allowed, c.wantAllowed, d)
			}
			if c.wantAllowed {
				if d.Role != c.wantRole {
					t.Fatalf("role=%q want %q", d.Role, c.wantRole)
				}
				if d.Mode != c.wantMode {
					t.Fatalf("mode=%q want %q", d.Mode, c.wantMode)
				}
			}
		})
	}
}
