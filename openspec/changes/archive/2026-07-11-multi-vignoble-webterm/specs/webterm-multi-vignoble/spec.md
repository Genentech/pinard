## ADDED Requirements

### Requirement: Vignoble-Scoped Links and Grants

The signed link and the gateway grant SHALL carry the vignoble so a request identifies
its target namespace. The signature MUST bind the vignoble (e.g. `sig =
hmac(vignoble|target|exp)`), and verification MUST reject a link/grant whose vignoble was
altered.

#### Scenario: Link carries the vignoble
- **WHEN** a link is produced for a session in vignoble `V`
- **THEN** the link encodes `V` and its signature covers `V`

#### Scenario: Grant carries the vignoble
- **WHEN** the gateway issues a grant for a target in vignoble `V`
- **THEN** the grant encodes `V` and the responder for `V` accepts it only if its own vignoble matches

#### Scenario: Altered vignoble rejected
- **WHEN** a link or grant is replayed with a different vignoble than it was signed for
- **THEN** signature verification fails and access is denied

### Requirement: Gateway Serves Multiple Vignobles

The gateway SHALL serve a set of vignobles rather than a single one. The served set is
either an explicit configured allowlist OR, when unset, derived from the
`pinard-vignobles` KV (any vignoble that has published an owner) so it self-maintains as
vignobles come and go. Per request the gateway MUST resolve the vignoble, bridge the
browser to that vignoble's NATS namespace (`pinard.<vignoble>.webterm.*`), and resolve
the owner for that vignoble. A request for a vignoble the gateway does not serve MUST be
denied (403).

#### Scenario: Route to the requested vignoble's namespace
- **WHEN** an authorized request targets a session in vignoble `V` that the gateway serves
- **THEN** the gateway bridges over `pinard.V.webterm.*` to `V`'s responder

#### Scenario: Multiple vignobles on one gateway
- **WHEN** a tenant runs several vignobles and opens sessions in different ones
- **THEN** the single gateway serves each, routing to the correct namespace per request

#### Scenario: Unserved vignoble denied
- **WHEN** a request targets a vignoble not in the gateway's served set
- **THEN** access is denied

#### Scenario: Owner resolved per vignoble
- **WHEN** deciding operator authorization for a request in vignoble `V`
- **THEN** the owner is the `pinard-vignobles` KV entry for `V`, matched to the Cognito `preferred_username`

### Requirement: Single Shared Secret Across a Tenant's Vignobles

For a single tenant, the gateway SHALL verify links and grants for all of that tenant's
vignobles with one shared `link_secret`/`grant_secret`. Per-vignoble binding comes from
the vignoble carried in the signature, NOT from separate per-vignoble keys. (Per-tenant
or per-vignoble secrets are provided by the separate multi-tenant capability.)

#### Scenario: One secret verifies all the tenant's vignobles
- **WHEN** the gateway serves vignobles `A` and `B` for one tenant
- **THEN** links/grants for both verify against the tenant's single shared secret
- **AND** the vignoble in each signature keeps `A` and `B` requests distinct

### Requirement: Responder Vignoble Binding

A responder SHALL accept only grants whose vignoble matches its own namespace, and MUST
NOT act on a grant scoped to a different vignoble.

#### Scenario: Responder rejects a foreign-vignoble grant
- **WHEN** a responder for vignoble `A` receives a request whose grant is scoped to `B`
- **THEN** it does not attach and does not answer
