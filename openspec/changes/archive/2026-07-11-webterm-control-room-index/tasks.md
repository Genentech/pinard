## 1. Live enumeration protocol

- [x] 1.1 `subjects.go`: `ListSubject(v)`; `ListReq{Grant}`; `ListReply{OK, Sessions, Windows}` (WinInfo{Index,Name}).
- [x] 1.2 `responder.go`: subscribe `ListSubject` in `Run`; verify grant (vignoble-scoped, silent on failure); `tmux list-sessions` + `list-windows -t conductor`; reply.

## 2. Owned-vignoble discovery

- [x] 2.1 `operator.go`: `OwnerStore.OwnedBy(username) []string` from `pinard-vignobles` KV (case-insensitive; cached).

## 3. Gateway index + APIs

- [x] 3.1 `/sessions` no-target + authed operator → serve `web/index.html`; keep terminal/viewer/login paths otherwise.
- [x] 3.2 `GET /sessions/api/vignobles` → owned vignobles JSON (auth + operator).
- [x] 3.3 `GET /sessions/api/sessions?v=` → require caller owns `v`; mint list grant; NATS list request; enrich vendangeurs from `pinard-agents` KV; return régisseur/maîtres/vendangeurs with targets.
- [x] 3.4 `web/index.html`: two-pane UI (sidebar + sessions), links to terminal views.

## 4. Tests, docs, ship

- [x] 4.1 Unit: `OwnedBy` filter; list-grant verify; `/api/sessions` non-owner → 403; unauth → login/401.
- [x] 4.2 Integration: scratch tmux (`conductor` + a worker session) → responder list → gateway `/api/sessions` returns them.
- [x] 4.3 `go build`/`vet`/`test`; `helm lint`/`template`; CLAUDE.md webterm section.
- [x] 4.4 Commit, push, open MR.
