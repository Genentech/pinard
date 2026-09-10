## Why

**Builds on `multi-vignoble-webterm`** (one gateway serving one tenant's vignobles via
vignoble-scoped links/grants). That change still uses **one shared secret**. Multi-tenancy
is the isolation layer on top: multiple **independent** operators/hosts share the gateway,
each with **their own** secrets, so one tenant can be cut off without forcing everyone else
(including you) to rotate secrets and lose access. Today revoking anyone means rotating the
one shared secret, which revokes everyone.

**Scope note:** vignoble routing (one gateway → a tenant's vignobles) is specified in
`multi-vignoble-webterm`. This change adds ONLY per-tenant secret isolation and revocation;
it does not re-specify vignoble routing.

## What Changes

- Introduce the **tenant** as the unit of ownership: a tenant = the operator of one
  pinard deployment. **host = tenant = user** — one tenant owns one `credentials.yaml`,
  portable across hosts (move the deployment, keep the creds). Not multiple end-users per
  host (viewers authenticate individually via Cognito; unchanged).
- **Per-tenant secrets**: each tenant has its own `link_secret` + `grant_secret`, sourced
  from that tenant's own Vault keys/path — never a globally shared pair. This replaces the
  single shared secret that `multi-vignoble-webterm` uses.
- **Per-tenant secret registry in the gateway**: resolve request → tenant (via the
  vignoble→tenant mapping from `multi-vignoble-webterm`) → that tenant's secret + owner.
- **Isolated revocation**: dropping one tenant's entry cuts that tenant's access with
  **zero collateral** to other tenants (no shared-secret rotation).
- **BREAKING** (internal): the gateway no longer verifies against one global secret; it
  selects the tenant's secret per request.
- **Design alternative to evaluate (not committed)**: asymmetric grants — the gateway signs
  grants with a private key and hosts hold only the public key, so a host never possesses a
  secret that could compromise another tenant, and revocation is the gateway refusing to
  sign for a vignoble.

## Capabilities

### New Capabilities
- `webterm-multi-tenancy`: The tenant model, per-tenant secrets, the gateway's per-tenant
  secret registry, and isolated per-tenant revocation — layered on the vignoble routing
  from `webterm-multi-vignoble`.

### Modified Capabilities
<!-- Depends on webterm-multi-vignoble (separate change) for vignoble-scoped links/grants
     and namespace routing; this change does not re-specify those. -->

## Impact

- **Gateway** (`internal/webterm/gateway.go`, `cmd/webterm-gateway`): per-request tenant
  resolution + per-tenant secret selection (extends the served-vignoble routing from
  `multi-vignoble-webterm`).
- **Responders** (`internal/webterm/responder.go`): verify with the tenant's own secret
  (from that host's `credentials.yaml`). No topology change.
- **Config** (`internal/config/credentials.go`): per-tenant secret sourcing; optional
  explicit tenant identifier.
- **Ops/Vault**: per-tenant Vault keys/paths; the gateway loads the set of tenants it
  serves; a documented revocation runbook.
- **Chart** (`charts/pinard-webterm-gateway`): supply per-tenant secrets (per-tenant
  ExternalSecrets) instead of one pair.
- **Docs**: CLAUDE.md web-terminal section — tenant terminology + isolation model.
