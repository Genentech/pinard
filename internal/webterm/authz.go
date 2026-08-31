package webterm

// Identity is the authenticated user derived from the Cognito ID token (Phase 2)
// or empty when auth is disabled (Phase 1 signed-link-only).
type Identity struct {
	Username string // preferred_username — the authorization/audit key
	Email    string
}

func (i Identity) Authenticated() bool { return i.Username != "" }

// Decision is the outcome of an authorization check.
type Decision struct {
	Allowed bool
	Mode    string // ModeRO / ModeRW
	Role    string // "operator" | "viewer"
	Reason  string // for deny; for audit on allow
}

// Authorize decides access to a tmux target (deny-by-default). authRequired is
// true when the gateway enforces Cognito (WebtermAuthEnabled): then an
// unauthenticated identity is denied. When authRequired is false (Phase-1
// fallback), a valid signed link alone suffices.
//
//   - operator (identity.Username == vignoble owner) → any target; writable
//     ("steer") when requestedWritable, else read-only
//   - funder (identity.Username == capsule contract funder's OIDC username) → target only;
//     always read-only (never ModeRW, even when requestedWritable)
//   - viewer → only a target scoped by a valid signed link, always read-only
//
// Writable is single-writer, operator-only, and must be explicitly requested.
func Authorize(id Identity, linkValid, isOperator, isFunder, authRequired, requestedWritable bool) Decision {
	if authRequired && !id.Authenticated() {
		return Decision{Reason: "authentication required"}
	}
	if isOperator {
		mode := ModeRO
		if requestedWritable {
			mode = ModeRW
		}
		return Decision{Allowed: true, Mode: mode, Role: "operator"}
	}
	if isFunder {
		// Capsule funder: read-only, always. Never grant steer even if requested.
		return Decision{Allowed: true, Mode: ModeRO, Role: "funder"}
	}
	if linkValid {
		return Decision{Allowed: true, Mode: ModeRO, Role: "viewer"}
	}
	return Decision{Reason: "not entitled to this session"}
}
