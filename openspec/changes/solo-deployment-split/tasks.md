> Blocking order: #212 (done) → #214 → #220 → #213 / #215 / #216
> Enterprise (k8s/charts) is unaffected by all tasks below.

## 1. Linux / dev / CI fallback (prerequisite, complete)

- [x] 1.1 **#212** (MR !411): single `pinard-services` Docker image with NATS, engram,
  SurrealDB, memory, and webterm-gateway under s6-overlay supervision.
  Status: **done**. Repurposed: this image is now the Linux / dev-in-container / CI
  solo backend and portable fallback — not the primary macOS path.
  s6-overlay dependency graph (`nats → engram + surreal → memory → webterm-gateway`,
  health-gated) is the reference the Swift `ServiceOrchestrator` mirrors.

## 2. Go memory rewrite — HARD PREREQUISITE for native macOS (#214)

- [ ] 2.1 Rewrite `services/memory/*` (ingester, recall_service, rollup, ontology
  gardener, wiki, SurrealDB client) as a single Go binary.
- [ ] 2.2 Produce a native darwin arm64 + amd64 binary (Universal or two slices) that
  the `ServiceOrchestrator` can embed and spawn.
- [ ] 2.3 Validate parity with the Python implementation: NATS JetStream ingestion,
  engram writes, SurrealDB queries, recall gRPC/HTTP surface.
- [ ] 2.4 Update the Docker image (`pinard-services`) to use the Go binary in place of
  the Python services. Validate the Linux fallback path still works end-to-end.
- [ ] 2.5 Remove Python runtime + py2app/bundler infrastructure from the repo after
  the Go binary is validated.

**Gate:** #220 (native macOS app) MUST NOT start until 2.2 is complete and the Go
memory binary is buildable for darwin.

## 3. Native macOS app + Swift ServiceOrchestrator (#220)

- [ ] 3.1 Scaffold the Xcode project: Swift app target, `SMAppService` launch-agent
  registration, menu-bar `NSStatusItem` with start/stop/status actions.
- [ ] 3.2 Implement `ServiceOrchestrator`: `Process`-based child management,
  dependency-ordered startup (NATS → engram + SurrealDB → memory → webterm-gateway),
  health probing (HTTP `/healthz` or TCP), exponential back-off.
- [ ] 3.3 Implement teardown: SIGTERM → 10 s grace → SIGKILL, reverse-order, no zombie
  processes; `NSTask.terminationHandler` for unexpected exits + auto-restart with
  back-off (max 3).
- [ ] 3.4 Embed darwin binaries in `Contents/Helpers/`: NATS, engram, SurrealDB,
  webterm-gateway, memory (Go binary from #214). Lipo Universal where available.
- [ ] 3.5 Implement port configuration (`~/Library/Application Support/Pinard/config.json`)
  and a preferences pane; expose defaults (NATS 4222, engram 7437, SurrealDB 8000,
  memory 9090, webterm-gateway 8080).
- [ ] 3.6 Pipe each child's stdout/stderr into rotating log files
  (`~/Library/Application Support/Pinard/logs/<service>.log`, max 10 MB) and the
  in-app diagnostic panel (SwiftUI sheet).
- [ ] 3.7 Data directories under `~/Library/Application Support/Pinard/data/`; created
  on first launch with `0700` permissions.
- [ ] 3.8 Code signing + notarization CI pipeline: sign each embedded binary
  individually (Developer ID Application), lipo, sign the `.app`, notarise, staple.
  Hardened Runtime (`--options runtime`) required on all binaries.
- [ ] 3.9 Package as `.dmg` (drag-to-Applications) and optionally `.pkg` for
  enterprise-managed deploys.

## 4. Wire the solo profile (#213)

- [ ] 4.1 `aoc init --local` writes a `solo` profile `credentials.yaml` with
  localhost endpoints: `nats.url: nats://127.0.0.1:4222`, `engram: 127.0.0.1:7437`,
  `surrealdb: ws://127.0.0.1:8000`, `webterm_gateway: http://127.0.0.1:8080`.
  BYO LLM key (pi direct provider), local `.env` secrets, no cloud, no proxy.
- [ ] 4.2 Port values read from the native app's `config.json` when present (macOS)
  or use defaults (Linux Docker path). Profile identical across both backends.
- [ ] 4.3 `aoc` connects only — it never supervises or health-checks these services.
- [ ] 4.4 End-to-end smoke test: `aoc init --local` + `pinard up` + spawn a worker +
  confirm memory ingestion + webterm accessible at `http://127.0.0.1:8080`.

## 5. `pinard up` / `pinard down` backend-aware lifecycle (#215)

- [ ] 5.1 `pinard up` on macOS: start the native `ServiceOrchestrator` app (or signal
  the running menu-bar app to start services), wait for health of all five services,
  then exit 0.
- [ ] 5.2 `pinard up` on Linux: `docker run --rm -d … pinard-services` with data
  volume + env; wait for container health (`docker inspect --format {{.State.Health}}`).
- [ ] 5.3 `pinard down` on macOS: signal the app to stop services (SIGTERM chain);
  wait for all processes to exit.
- [ ] 5.4 `pinard down` on Linux: `docker stop pinard-services`.
- [ ] 5.5 No-auth local webterm at `http://127.0.0.1:8080` (Cognito/SSO disabled),
  served by webterm-gateway in both backends.
- [ ] 5.6 `pinard status`: show each service's health (up/down/port) for both backends.

## 6. Docs (#216)

- [ ] 6.1 Site docs ("The Craft"): **"Full Pinard on your Mac in 5 minutes"** — native
  macOS path: install the `.app` → `aoc init --local` → `pinard up` → BYO Anthropic key
  → GitLab prerequisite. Note that agents run in the user's shell.
- [ ] 6.2 **Linux fallback doc**: `docker run pinard-services` + `aoc init --local`;
  optional `docker-compose.yml` wrapper. `pinard up` on Linux drives Docker.
- [ ] 6.3 Architecture diagram: two-tier split (enterprise k8s / solo native macOS +
  Linux Docker fallback), agent shell ↔ services loopback boundary.
- [ ] 6.4 Troubleshooting: port conflicts, notarization Gatekeeper quarantine, log
  locations, migration from Docker volume to Application Support.
- [ ] 6.5 Enterprise k8s path: note it is unchanged; cross-link to existing chart docs.

## 7. Validation

- [ ] 7.1 **macOS acceptance**: clean Mac (no Docker, no k8s) — install the `.app` →
  services come up as native processes → `aoc init --local` + `pinard up` → working
  Pinard (orchestration + full memory + webterm over localhost) with BYO key +
  agents running in the user's shell against real repos.
- [ ] 7.2 **Linux fallback acceptance**: `docker run pinard-services` + `aoc init --local`
  → same localhost endpoints → working end-to-end.
- [ ] 7.3 **Enterprise unchanged**: existing k8s deployment and charts unaffected.
