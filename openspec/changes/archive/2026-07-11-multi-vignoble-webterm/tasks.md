## 1. Vignoble in link & grant

- [x] 1.1 `link.go`: add `v` (vignoble) to the URL + fold into the HMAC (`sig = hmac(v|target|exp)`); update `BuildLink`/`VerifyLink` signatures.
- [x] 1.2 `grant.go`: add `Vignoble` to `Grant`; it's covered by `SignGrant`/`VerifyGrant` (JSON payload).
- [x] 1.3 Unit tests: vignoble roundtrip; altered-vignoble/tamper/expiry rejected.

## 2. Gateway namespace routing

- [x] 2.1 Replace `Gateway.Vignoble` with `Vignobles` (served set); helper to check membership.
- [x] 2.2 Per request: read `v`, deny if unserved, verify link (shared secret), resolve owner via `OwnerStore.IsOperator(v, …)`, and use `v` for `ReqSubject`/`Out`/`Ctl`/`Evt`.
- [x] 2.3 Mint the grant with `Vignoble=v`.
- [x] 2.4 `cmd/webterm-gateway/main.go`: build the served set from config; audit logs the vignoble.

## 3. Responder + link builders

- [x] 3.1 `responder.go`: after grant verify, require `grant.Vignoble == r.Vignoble`, else stay silent.
- [x] 3.2 `aoc webterm-link --vignoble` (default resolved vignoble); include `v` in the URL.
- [x] 3.3 `aoc track-mr`: include the worker's vignoble in the posted link.

## 4. Config & chart

- [x] 4.1 Config: `webterm` served-vignoble list (or gateway reads it from chart env); back-compat with single `vignoble`.
- [x] 4.2 Chart: `gateway.vignobles` in values/configmap/custom-dev/uat (exohub + the others).

## 5. Tests, build, docs

- [x] 5.1 Go unit: served-set membership + per-vignoble authz; unserved-vignoble deny; responder foreign-vignoble reject.
- [x] 5.2 Integration: two vignobles/namespaces through one gateway, each streams its own.
- [x] 5.3 `go build ./...`, `go vet`, `go test ./internal/... ./cmd/aoc/`; `helm lint`/`template`; extension typecheck if touched.
- [x] 5.4 Docs: CLAUDE.md web-terminal section — one gateway, many vignobles (single tenant).
