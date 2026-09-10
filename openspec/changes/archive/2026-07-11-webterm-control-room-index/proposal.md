## Why

With multi-vignoble + Cognito auth live, an operator can view any of their sessions — but
only by knowing a session name and hand-crafting `…/sessions?v=&target=`. There is no way
to *discover* what's running. Phase 3 adds an authenticated **control-room index**: a
left sidebar of the operator's vignobles; click one to see its live sessions (régisseur,
maître windows, vendangeurs) as links to read-only views.

## What Changes

- **Index page** at `/sessions` (no `target`) for authenticated **operators**: a two-pane
  UI — sidebar of owned vignobles, main pane of the selected vignoble's live sessions.
- **Live enumeration over NATS**: responders answer a new list request with
  `tmux list-sessions` / `list-windows -t conductor`; the gateway aggregates and enriches
  vendangeur rows with parcelle/state from the `pinard-agents` KV.
- **Owned-vignoble discovery**: the gateway lists the vignobles whose owner matches the
  operator's `preferred_username` (from the `pinard-vignobles` KV).
- **Operator-gated**: the index and its APIs require a verified identity that owns the
  vignoble; viewers (scoped-link holders) are unaffected and never see the index.
- Each row links to the existing read-only terminal view.

## Capabilities

### New Capabilities
- `webterm-control-room`: Authenticated, operator-scoped index of live tmux
  sessions/windows across a tenant's vignobles, enumerated over NATS, linking to
  read-only terminal views.

### Modified Capabilities
<!-- None: builds on the merged web-terminal-access + multi-vignoble behavior. -->

## Impact

- `internal/webterm/subjects.go`: `ListSubject` + list request/reply types.
- `internal/webterm/responder.go`: list handler (grant-verified `tmux list-*`).
- `internal/webterm/operator.go`: `OwnerStore.OwnedBy(username)`.
- `internal/webterm/gateway.go`: index page + `/sessions/api/{vignobles,sessions}`
  (operator-gated); reuses the auth middleware + `OwnerStore`.
- `internal/webterm/web/index.html`: new two-pane frontend.
- Docs: CLAUDE.md web-terminal section.
- **Deferred:** per-window grouped-session pinning (read-only `attach -r` to a maître
  window can still move the operator's active window until Phase 4); writable steer.
