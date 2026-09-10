## Why

The web-terminal gateway is pinned to a single vignoble (`gateway.vignoble: exohub`):
it bridges only `pinard.exohub.webterm.*` and can view exohub sessions only. A single
operator (tenant) runs several vignobles (`exohub`, `example`, `litsum`, `misc`,
`targetnexus`) and wants **one gateway** (`pinard.example.com/sessions`) to reach
all of them. This is distinct from multi-tenancy (multiple operators/hosts with isolated
secrets) — here it is one tenant, one secret, many vignobles; the gateway just needs to
stop being single-namespace.

## What Changes

- The signed link and the gateway grant **carry the vignoble**, so a request identifies
  which vignoble/namespace it targets.
- The gateway **serves a set of vignobles** instead of one: per request it resolves the
  vignoble, routes the NATS bridge to that namespace (`pinard.<v>.webterm.*`), and looks
  up the owner for that vignoble (already per-vignoble in the `pinard-vignobles` KV).
- Verification stays with the tenant's **single shared** `link_secret`/`grant_secret`
  (the vignoble is bound into the signature, not a per-vignoble key — per-tenant/
  per-vignoble secrets are the separate multi-tenant change).
- The responder rejects a grant whose vignoble does not match its own namespace.
- **BREAKING** (internal): links/grants gain a vignoble field; the old single-vignoble
  format is superseded. Short-TTL links make this low-impact; a transitional default
  vignoble may be accepted during rollout.

## Capabilities

### New Capabilities
- `webterm-multi-vignoble`: One gateway serving multiple vignobles for a single tenant —
  vignoble-scoped links/grants, per-request namespace + owner routing, and a configured
  set of served vignobles, using the tenant's shared secret.

### Modified Capabilities
<!-- web-terminal-access is not archived to openspec/specs/; its single-vignoble
     behavior is superseded by webterm-multi-vignoble rather than via a delta. -->

## Impact

- `internal/webterm/link.go`, `grant.go`: add vignoble to the signed link + grant.
- `internal/webterm/gateway.go`, `cmd/webterm-gateway`: served-vignoble set; per-request
  vignoble resolution → subjects + `OwnerStore` lookup; single shared secret retained.
- `internal/webterm/responder.go`: verify the grant's vignoble matches its namespace.
- `cmd/aoc/cmd_webterm.go` (`webterm-link --vignoble`), `cmd/aoc/cmd_track_mr.go`
  (include the worker's vignoble in the posted link).
- `charts/pinard-webterm-gateway`: `gateway.vignobles` served-set (values/configmap/custom).
- Docs: CLAUDE.md web-terminal section.
- Underpins the separate `multi-tenant-webterm` change (per-tenant secrets + isolated
  revocation build on this).
