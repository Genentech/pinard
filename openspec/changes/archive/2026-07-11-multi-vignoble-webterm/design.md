## Context

The gateway (`internal/webterm/gateway.go`) holds a single `Vignoble` and one
`LinkSecret`/`GrantSecret`; `link.go`/`grant.go` sign `target|exp` with no vignoble; the
responder verifies grants with its own namespace's secret. A single tenant (`example-user`)
runs five vignobles on this host, all sharing one `credentials.yaml` (one secret pair),
each with its own NATS namespace + daemon/responder. Goal: one gateway serving all of
them. This is single-tenant multi-vignoble — no per-tenant secret isolation (that is the
separate `multi-tenant-webterm` change).

## Goals / Non-Goals

**Goals:**
- One gateway serves a configured set of the tenant's vignobles.
- Links/grants carry the vignoble; the gateway routes namespace + owner per request.
- Reuse the tenant's single shared secret; responder binds grants to its vignoble.

**Non-Goals:**
- Per-tenant / per-vignoble secrets and isolated revocation (→ multi-tenant change).
- Multiple end-users per host (Cognito already handles per-user auth).
- Control-room index / writable steer (separate phases).

## Decisions

### D1 — Vignoble in the link and grant
Add a `v` (vignoble) field to the link query and fold it into the HMAC:
`sig = hmac(v|target|exp)` (`link.go`). Add `Vignoble` to the `Grant` struct signed by
`SignGrant`/verified by `VerifyGrant` (`grant.go`). Both use the tenant's single shared
secret; the vignoble is bound by the signature, not by key selection.

### D2 — Gateway served-vignoble set + per-request routing
Replace `Gateway.Vignoble` with `Vignobles` (a served set). Per request:
1. read `v` from the link; deny if `v` not in the served set;
2. verify the link with the shared `LinkSecret` (sig covers `v`);
3. resolve owner via `OwnerStore.IsOperator(v, username)` (already per-vignoble);
4. mint the grant with `Vignoble=v`, request `ReqSubject(v)`, bridge `OutSubject(v,id)` /
   `CtlSubject(v,id)` / `EvtSubject(v,id)`.
The served set comes from config (`gateway.vignobles`). **When empty (preferred), the
served set is derived from the `pinard-vignobles` KV** — the gateway serves any vignoble
that has published an owner (i.e., has a live responder). This is self-maintaining: a new
vignoble's daemon publishes on startup, so no gateway redeploy is needed; an unknown
vignoble gets a clean 403. An explicit list is only for pinning a fixed allowlist.

### D3 — Responder binds grants to its namespace
The responder already runs per namespace (`Responder.Vignoble`). In `handleRequest`, after
verifying the grant, also require `grant.Vignoble == r.Vignoble`; otherwise stay silent
(consistent with the silent-on-unverifiable-grant behavior) so a grant for another
vignoble is never acted on. With one shared secret this is the guard that keeps vignobles
distinct on the wire.

### D4 — Link/grant builders carry the vignoble
`aoc webterm-link` gains `--vignoble` (defaults to the resolved vignoble); `aoc track-mr`
includes the worker's vignoble in the posted link. The gateway's grant TTL/mint is
unchanged apart from the new field.

### D5 — Backward compatibility
New vignoble-scoped format supersedes the old. Because links are short-TTL, enforce the
new format; optionally accept a legacy no-`v` link mapped to a configured default vignoble
during rollout, then remove.

## Risks / Trade-offs

- **Shared secret across the tenant's vignobles** → compromise of the secret exposes all
  of that tenant's vignobles. Acceptable for one tenant; per-vignoble/per-tenant secrets
  are the multi-tenant change.
- **Serving a vignoble with no responder** (e.g. no daemon for it) → requests get no
  reply → "session not found". Graceful; operator runs a daemon/responder for vignobles
  they want viewable.
- **Cross-vignoble replay** → prevented by binding the vignoble into the signature and the
  responder's namespace check.
