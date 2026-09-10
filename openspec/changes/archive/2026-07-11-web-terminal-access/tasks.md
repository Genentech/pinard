> **Status:** COMPLETE — Phase 1 (signed-link, read-only, over NATS), Phase 2
> (in-gateway Cognito OIDC + operator discovery), Phase 3 (control-room index),
> and Phase 4 (writable "steer" + grouped-session navigation) are all implemented,
> merged, and deployed. Ready to archive. See `design.md` §Migration Plan.

## 1. Gateway skeleton + Cognito auth (k8s service)

- [x] 1.1 Scaffold the gateway service (Go, `internal/webterm` + `cmd/webterm-gateway`; deployable like engram) with config: signing secrets, NATS creds, idle/concurrency limits. *(identity→role mapping deferred to P2 — no SSO yet.)*
- [x] 1.2 Cognito authentication: **in-gateway** OIDC (Authorization Code + PKCE, public client) — discovers endpoints from the issuer, validates the ID token against the pool JWKS (`iss`/`aud`/`exp`/`token_use`), rejects invalid/expired. Not oauth2-proxy/Traefik (chosen: gateway does it). *(`internal/webterm/auth.go`)*
- [x] 1.3 Identity: derive `preferred_username` (fallback `cognito:username`) + `email`; the AD `groups` claim is not used for authz.
- [x] 1.4 Operator discovery: daemon + standalone responder publish `vignoble → owner` to the `pinard-vignobles` KV (owner = `credentials.yaml` `nats.user` or the `owner:` override); gateway grants operator role on `preferred_username` match (case-insensitive; no manual mapping). *(`internal/webterm/operator.go`)*
- [x] 1.5 Authorization helper (deny-by-default): operator → any target; viewer → signed-link-scoped target; unauthenticated → denied (when auth enabled). *(`internal/webterm/authz.go`)*
- [x] 1.6 Audit log: identity (verified username, else `link`), role, target, timestamp, read-only/writable per access.

## 2. NATS terminal transport (gateway bridge + host responder)

- [x] 2.1 Define the per-viewer NATS subject scheme (unique output/input/control subjects) + a signed short-lived gateway grant (HMAC, shared secret) encoding target + mode + expiry. *(`internal/webterm/subjects.go`, `grant.go`)*
- [x] 2.2 Gateway side: bridge the browser WebSocket ↔ NATS (output→WS, resize→control); teardown on disconnect/idle. *(`internal/webterm/gateway.go`; input→responder wired but read-only drops it in P1.)*
- [x] 2.3 Terminal-responder (Go, on tmux host + standalone worker launcher): subscribe to terminal-request subjects, verify the gateway grant, spawn `tmux attach -r` via embedded PTY (`creack/pty`), stream frames over **core NATS (never JetStream)**; refuse requests without a valid grant. *(`internal/webterm/responder.go`, `aoc webterm-responder`, daemon goroutine, `bin/pinard` standalone hook)*
- [x] 2.4 Flow control: responder-side rate-cap + frame coalescing + bounded buffer (runaway `cat`/`yes` can't flood); live-viewer concurrency limits. *(dedicated NATS account/connection is a follow-up — P1 uses the shared connection with subject separation.)*
- [x] 2.5 Responder lifecycle: tear down the PTY on viewer disconnect/idle and when the tmux target exits (no orphans); validate target via `internal/session` (`SanitizeName`, existence).

## 3. Read-only view end-to-end

- [x] 3.1 xterm.js frontend page served by the gateway (single-session view), wired to the WebSocket. *(`internal/webterm/web/term.html` + vendored xterm, `go:embed`)*
- [x] 3.2 End-to-end read-only path: browser → gateway → NATS → responder → `tmux attach -r` → back; resize applied; keyboard input dropped in read-only. *(covered by `integration_test.go`)*
- [x] 3.3 Clear "session not found / ended" handling when no responder answers.

## 4. Signed links + vendangeur posts

- [x] 4.1 Signed-URL builder (HMAC, shared secret): `target`, `exp`, `sig`; gateway verification (signature + expiry + scope). *(Cognito still deferred — P2.)*
- [x] 4.2 Vendangeur posts its session link (`…/sessions?target=…&exp=…&sig=…`) on the MR (Go-side in `aoc track-mr`, on first tracking).

## 5. Control-room navigation

- [x] 5.1 Authed index page: list entitled sessions/windows (régisseur, maîtres via `list-windows -t conductor`, vendangeurs via `list-sessions`) — enumerated via responder(s) over NATS; hide non-entitled.
- [x] 5.2 Operator-role gate for the index + control-room targets; link each entry to its read-only view.

## 6. Writable steer + non-disruptive navigation

- [x] 6.1 Writable mode gated on the operator/steer entitlement; explicit request; falls back to read-only; audited as writable; responder honors input subject only when the grant is writable.
- [x] 6.2 Grouped per-viewer sessions (`tmux new-session -t <session>`) pinned to a window so read-only viewers navigate windows without disturbing the operator.

## 7. Deploy, audit, docs, tests

- [x] 7.1 Exposure: Traefik `PathPrefix(/sessions)` on `Host(pinard.example.com)` (`web` entrypoint) → gateway service. *(chart `charts/pinard-webterm-gateway` authored; SSO in front is P2.)*
- [x] 7.2 Responder rollout: run on the pinard host (daemon-managed) and `aoc webterm-responder` + standalone/HPC launcher hook; shared secrets + NATS creds via `credentials.yaml`/Vault.
- [x] 7.3 Tests: Go unit (signed-link verify/expiry/tamper, grant verify/expiry/tamper) + responder grant-rejection; integration test streaming a scratch tmux session over NATS read-only. *(JWT/JWKS/authz-matrix tests are P2.)*
- [x] 7.4 `go build ./...`, `go vet`, `go test ./internal/... ./cmd/aoc/`. *(no extension `.ts` changed.)*
- [x] 7.5 Docs: CLAUDE.md section (gateway + responder, NATS transport, link flow, secrets, limitations). *(Cognito/control-room docs land with P2/P3.)*
