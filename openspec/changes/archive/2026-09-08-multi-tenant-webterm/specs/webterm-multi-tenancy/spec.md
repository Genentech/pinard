## ADDED Requirements

### Requirement: Tenant Model

A **tenant** SHALL be the unit of ownership for web-terminal access: the operator of
one pinard deployment. host = tenant = user — one tenant owns one `credentials.yaml`
and identity, portable across hosts. Multiple end-users are NOT provisioned per host;
individual viewers continue to authenticate per-user via Cognito (unchanged).

#### Scenario: One tenant owns many vignobles
- **WHEN** a tenant operates several vignobles (e.g. `exohub`, `example`) on their host
- **THEN** all of those vignobles resolve to that single tenant identity and secrets

#### Scenario: Tenant identity is portable across hosts
- **WHEN** a tenant moves their deployment to a different host but keeps the same `credentials.yaml`
- **THEN** the tenant identity, secrets, and vignoble ownership are unchanged

#### Scenario: Viewers are not tenants
- **WHEN** a person opens a terminal to watch a session
- **THEN** they authenticate individually via Cognito and need no tenant `credentials.yaml`

### Requirement: Per-Tenant Secrets

Each tenant SHALL have its own `link_secret` and `grant_secret`, sourced from that
tenant's own secret store (Vault keys/path). The gateway MUST NOT rely on a single
globally shared secret pair across tenants.

#### Scenario: Independent tenant secrets
- **WHEN** two tenants are served by the same gateway
- **THEN** each tenant's links and grants are signed/verified with that tenant's own secrets
- **AND** one tenant's secret cannot validate another tenant's link or grant

#### Scenario: Secret sourced per tenant
- **WHEN** the gateway serves a tenant
- **THEN** it loads that tenant's `link_secret`/`grant_secret` from the tenant's own Vault keys, not a shared key

### Requirement: Tenant Resolution and Secret Selection

The gateway SHALL resolve each request's vignoble (carried in the link/grant per the
`webterm-multi-vignoble` capability) to its **tenant**, and verify/sign with **that
tenant's** secret. Vignoble-scoping of links/grants itself is specified by
`webterm-multi-vignoble` and is not redefined here.

#### Scenario: Verify with the resolving tenant's secret
- **WHEN** a request for vignoble `V` (owned by tenant `T`) arrives
- **THEN** the gateway verifies its link and mints its grant using `T`'s secret

#### Scenario: Cross-tenant secret does not validate
- **WHEN** a link/grant signed by tenant `A`'s secret is presented for a vignoble owned by tenant `B`
- **THEN** verification fails (B's secret does not match)

### Requirement: Gateway Per-Tenant Registry

The gateway SHALL maintain a registry mapping `vignoble → { tenant, owner,
link_secret, grant_secret }`. The owner is discovered from the `pinard-vignobles` KV;
the per-tenant secrets are loaded from each tenant's secret source. A request for a
vignoble absent from the registry MUST be denied.

#### Scenario: Registry resolves a served vignoble
- **WHEN** a request references a vignoble present in the registry
- **THEN** the gateway uses that entry's secrets, owner, and namespace to authorize and bridge

#### Scenario: Unknown vignoble denied
- **WHEN** a request references a vignoble not in the registry
- **THEN** the gateway denies access (no secret to verify against)

#### Scenario: Owner still from KV
- **WHEN** resolving operator authorization for a vignoble
- **THEN** the owner comes from the `pinard-vignobles` KV entry for that vignoble, matched against the Cognito `preferred_username`

### Requirement: Isolated Per-Tenant Revocation

Revoking a tenant SHALL remove only that tenant's access, with no collateral to other
tenants. Removing a tenant's registry entry (and/or its secrets) MUST NOT require any
other tenant to rotate secrets or re-authenticate.

#### Scenario: Revoke one tenant
- **WHEN** an operator removes tenant `T`'s registry entry
- **THEN** all of `T`'s vignobles become inaccessible through the gateway
- **AND** every other tenant's access continues unaffected

#### Scenario: No shared-secret rotation on revocation
- **WHEN** a tenant is revoked
- **THEN** no other tenant's `link_secret`/`grant_secret` changes
