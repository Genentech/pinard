> **Depends on `multi-vignoble-webterm`** (vignoble-scoped links/grants + namespace
> routing). This change adds ONLY per-tenant secret isolation on top; it does not
> re-implement vignoble routing.

## 1. Prerequisite (in multi-vignoble-webterm, not here)

- [x] 1.1 Vignoble in link/grant + gateway namespace routing land in `multi-vignoble-webterm`.

## 2. Gateway per-tenant registry

- [ ] 2.1 Extend the served-vignoble routing into a registry `vignoble → { tenant, owner, linkSecret, grantSecret }` (secrets now per-tenant, not one shared pair).
- [ ] 2.2 Per-request: resolve vignoble → tenant entry; verify link + mint grant with the entry's secret; deny unknown vignoble.
- [ ] 2.3 Operator authz per entry: owner from `pinard-vignobles` KV (reuse `OwnerStore`), matched to Cognito `preferred_username`.
- [ ] 2.4 Mint grants per-vignoble with the resolved tenant's secret; audit logs tenant + vignoble + identity + role + mode.
- [ ] 2.5 Registry refresh + explicit reload path so revocation takes effect without a full redeploy.

## 3. Per-tenant secret sourcing

- [ ] 3.1 Config/loader for the set of tenants the gateway serves, each with its own `link_secret`/`grant_secret` source (per-tenant Vault keys/path).
- [ ] 3.2 Host side unchanged: each tenant's `credentials.yaml` holds only its own secrets; responder verifies with them.
- [ ] 3.3 Chart: supply per-tenant secrets (per-tenant ExternalSecrets / key set) instead of one pair; document adding/removing a tenant.

## 4. Revocation

- [ ] 4.1 Implement revoke = remove tenant registry entry (+ optional Vault key removal); verify other tenants unaffected.
- [ ] 4.2 Ops runbook: onboard a tenant (Vault keys + registry entry) and revoke a tenant.

## 5. Backward compatibility & rollout

- [ ] 5.1 Optional transitional acceptance of legacy no-vignoble links mapped to a default tenant, config-gated; remove after rollout.
- [ ] 5.2 Migrate the current single-tenant (`example-user`/`exohub`) deploy onto the registry; confirm all five vignobles reachable through one gateway.

## 6. Asymmetric grants (design spike, optional)

- [ ] 6.1 Prototype gateway-private-key grant signing + host public-key verification; assess link-signing changes (per-tenant keypair vs gateway-side minting).
- [ ] 6.2 Decide whether to adopt; if so, structure the registry verifier to swap symmetric → asymmetric.

## 7. Tests, docs

- [ ] 7.1 Go unit: registry resolution + authz matrix across tenants; unknown-vignoble deny; revocation isolation.
- [ ] 7.2 Integration: two tenants/vignobles over NATS through one gateway; each streams only with its own secret.
- [ ] 7.3 `go build ./...`, `go vet`, `go test ./internal/... ./cmd/aoc/`; `helm lint`/`template`.
- [ ] 7.4 Docs: CLAUDE.md web-terminal section — tenant terminology + multi-tenant model + revocation runbook.
