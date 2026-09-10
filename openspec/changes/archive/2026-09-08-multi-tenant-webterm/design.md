## Context

web-terminal-access (Phases 1–2, merged) gives one gateway (`pinard.example.com
/sessions`) a signed-link read-only terminal over NATS + in-gateway Cognito OIDC/PKCE
auth + per-vignoble operator discovery via the `pinard-vignobles` KV. It is
**single-tenant**: the gateway loads one `link_secret`/`grant_secret` (`Gateway.LinkSecret`
/`GrantSecret` in `internal/webterm/gateway.go`) and one `Vignoble`; links/grants
(`link.go`/`grant.go`) do not carry a vignoble. Deployment today: one tenant (`example-user`),
five vignobles, gateway pinned to `exohub`.

We want one gateway to serve **all of a tenant's vignobles and multiple tenants**, with
**per-tenant secrets** so one tenant can be revoked without others rotating. Terminology:
**tenant** = deployment operator; host = tenant = user; portable `credentials.yaml`.

## Goals / Non-Goals

**Goals:**
- One gateway serves many vignobles across many tenants.
- Per-tenant `link_secret`/`grant_secret`; no globally shared pair.
- Links + grants carry the vignoble → resolve tenant, namespace, secret, owner.
- Revoke a tenant with zero collateral to others.
- Keep Cognito per-user auth and `pinard-vignobles` owner discovery unchanged.

**Non-Goals:**
- Multiple end-users per host (viewers are per-user via Cognito already).
- Changing responder topology (still one responder per host/namespace).
- Writable steer / control-room index (separate phases).
- A tenant self-service onboarding UI (ops runbook for now).

## Decisions

### D1 — Vignoble is the routing key; tenant is derived from it
Every request already targets a session in a vignoble. Add the **vignoble** to the
signed link and the grant, and have the gateway resolve `vignoble → tenant` via its
registry. This reuses the existing per-vignoble owner KV and per-namespace responders;
no new client-facing identifier is needed. A distinct tenant id is optional metadata.

### D2 — Gateway per-tenant registry
Replace the single `LinkSecret`/`GrantSecret`/`Vignoble` fields with a registry:
`vignoble → { tenant, owner(from KV), linkSecret, grantSecret, namespace }`. Per-request
the gateway looks up the vignoble, verifies the link/grant with that entry's secret,
checks operator via that entry's owner, and bridges over that namespace. Unknown
vignoble → deny (no secret to verify against). Registry is built from a configured set
of tenants; owner is refreshed from KV (existing `OwnerStore`).

### D3 — Per-tenant secret sourcing (Vault)
Each tenant's `link_secret`/`grant_secret` live under that tenant's own Vault keys/path
(e.g. per-tenant `webterm_link_secret`/`webterm_grant_secret`). The gateway loads the
set of tenants it serves. Revocation = remove the tenant's registry entry (and/or its
Vault keys); no other tenant's secret changes. The host side is unchanged: each tenant's
`credentials.yaml` holds only its own secrets.

### D4 — Symmetric now, asymmetric as a future hardening (alternative)
Keep symmetric per-tenant secrets for this change (simplest; already satisfies isolated
revocation). Document the **asymmetric** option as a follow-up: the gateway signs grants
with a private key; hosts hold only the public key, so a host never holds a secret that
could compromise another tenant, and revocation is the gateway refusing to sign for a
vignoble. Links are host-signed → gateway-verified, so asymmetric links would need
per-tenant keypairs or moving link minting to the gateway. Deferred — noted so the
symmetric registry is structured to allow swapping the verifier later.

### D5 — Backward compatibility
Vignoble-scoped links/grants are a new format. Since links are short-TTL, enforce the
new format directly (old links expire fast). During rollout the gateway may accept a
legacy no-vignoble link mapped to a default tenant, gated by config, then removed.

## Risks / Trade-offs

- **Gateway holds every served tenant's secret** (symmetric) → blast radius if the
  gateway is compromised. Mitigate: least-privilege Vault reads, short grant TTLs,
  audit; asymmetric grants (D4) remove host-held secrets entirely later.
- **Registry staleness** → a revoked tenant lingering until reload. Mitigate: KV-driven
  owner refresh + bounded registry refresh / explicit reload on revocation.
- **Misrouting across tenants** → strict vignoble binding in link+grant; cross-vignoble
  replay fails verification (different tenant secret).
- **Onboarding friction** → per-tenant Vault keys + registry entry is manual; acceptable
  for a small number of tenants, revisit with self-service if it grows.
