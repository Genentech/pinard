## Context

Merged: multi-vignoble gateway (`Gateway.serves`, per-request `v`), Cognito auth
(`internal/webterm/auth.go`, `identify`), operator discovery (`OwnerStore` over the
`pinard-vignobles` KV). The `pinard-agents` KV already holds every live vendangeur
(name/parcelle/state/vignoble). Missing: a way to *discover* sessions — an authed index.
The user chose live NATS enumeration and a two-pane (sidebar + sessions) UI.

## Goals / Non-Goals

**Goals:** operator-only index; sidebar of owned vignobles; live per-vignoble session list
(régisseur, maîtres, vendangeurs) over NATS; each row links to the existing read-only view.

**Non-Goals:** per-window grouped-session pinning (Phase 4), writable steer, viewer-facing
index (viewers use scoped links).

## Decisions

### D1 — Live enumeration via a new responder request
New subject `pinard.<v>.webterm.list`. The responder (already per-namespace) answers with
`tmux -L pinard-<v> list-sessions` + `list-windows -t conductor`. The request carries a
gateway grant (vignoble-scoped, `Mode:"list"`); the responder verifies it and, as with the
viewer request, **stays silent** on an unverifiable/foreign-vignoble grant. Read-only and
cheap; reuses the request/reply + grant machinery.

### D2 — Two-pane index served at `/sessions`
`/sessions` with no `target`: if the caller is an authenticated operator, serve
`web/index.html` (sidebar + main pane). With a `target` it keeps the existing terminal
behavior; a viewer with a scoped link is unaffected. Two JSON APIs back the UI:
`/sessions/api/vignobles` (owned) and `/sessions/api/sessions?v=` (live list). The page is
plain HTML/JS (no build step), matching the embedded xterm assets.

### D3 — Owned-vignoble discovery from KV
`OwnerStore.OwnedBy(username)` = `KV.Keys("pinard-vignobles")` → `Get` each → keep those
whose `owner` equals the username (case-insensitive). Cached like `Owner`. The sidebar and
every API authorize against this.

### D4 — Enrichment from pinard-agents KV
The responder returns tmux names only. The gateway enriches vendangeur rows with
parcelle/state by matching the session name against `pinard-agents` KV records (reuse
`Owners.KV`). Régisseur/maître come from the conductor windows.

### D5 — Targets
Régisseur → `conductor`; maître window → `conductor:<window-index>`; vendangeur →
`<session>`. Each becomes a link to `/sessions?v=&target=` (the existing read-only view).

## Risks / Trade-offs

- **Maître window read-only view** via `conductor:<w>` can move the operator's active
  window (no grouped sessions yet) — documented; Phase 4 fixes it.
- **Enumeration only covers live responders** — vignobles without one degrade to
  régisseur/empty, not an error.
- **API authorization** must gate on owned-vignoble (deny-by-default) so an operator can't
  enumerate another tenant's vignoble.
