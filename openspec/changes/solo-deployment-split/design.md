## Context

The existing solo path (`pinard-services`, #212 / MR !411) uses a single Docker image
with s6-overlay supervision to bundle NATS, engram, SurrealDB, and the Python memory
layer. On macOS this requires Docker Desktop + a hypervisor Linux VM. The new native
macOS path removes this dependency by running the services tier as native child
processes of a Swift app.

Enterprise (Kubernetes) is unchanged throughout this design.

## Sandbox Analysis

### Design (a) — Services-only app, agents in the user's shell [ADOPTED]

The macOS app supervises **only** the infra service binaries as child processes. The
agent runtime (`aoc`, conductor, workers = `pi`, babysitter, git, compilers) runs in
the **user's normal shell** and connects to those services over `127.0.0.1` — the same
way `aoc` connects to the k8s cluster today (only endpoints differ).

**Rationale for not sandboxing agents:**
- A coding-agent tool reads and writes the user's real repository trees.
- It invokes arbitrary dev tooling: compilers, formatters, package managers, test
  runners, shell commands.
- A hard macOS App Sandbox confines file I/O to a container path (`~/Library/Containers/`)
  and forbids arbitrary subprocess execution via entitlements restrictions.
- Making the agent sandbox-compatible would require allowing `com.apple.security.files.all`
  and `com.apple.security.temporary-exception.files.absolute-path.read-write.*` — at
  which point the sandbox provides no meaningful protection.
- Therefore: **no sandbox on agents**. Agents run in the user's shell with the user's
  full file-system permissions.

The service processes (NATS, engram, SurrealDB, webterm-gateway, memory) are well-behaved
daemons that only need access to their own data directory under Application Support and a
handful of loopback ports. An optional per-service sandbox (e.g. `com.apple.security.network.server`
+ explicit read-write path entitlement scoped to `~/Library/Application Support/Pinard/`)
is feasible and desirable as a future hardening step, but is orthogonal to the current
design.

### Design (b) — Full App Sandbox hosting everything [REJECTED]

The app hosts the agent runtime (pi, aoc, babysitter) alongside the services under a
hard App Sandbox.

**Rejected because:**
- Arbitrary repo read/write is irreconcilable with a container-path sandbox without
  granting entitlements that remove all practical protection.
- Notarization of a Python-bundled memory layer (py2app/PyInstaller + per-arch native
  wheels) was the original path; it is fragile and maintenance-heavy.
- Running git, compilers, npm, etc. inside the sandbox requires `allow-unsigned-executable-memory`
  or similar entitlements that Apple's notarization policy increasingly restricts.

## Service Inventory and Native-Binary Status

| Service | Language | Darwin binary | Status |
|---------|----------|---------------|--------|
| NATS (`nats-server`) | Go | `nats-server` (arm64 + amd64) | ✅ Native — embed as Universal binary |
| engram | Go | `engram` (arm64 + amd64) | ✅ Native — embed as Universal binary |
| SurrealDB | Rust | `surreal` (arm64 + amd64) | ✅ Native — embed as Universal binary |
| webterm-gateway | Go (in-repo) | built from `cmd/webterm-gateway` | ✅ Native — built at release time |
| `services/memory` | Python | ❌ py2app bundle = blocker | 🔴 Blocker — resolved by #214 (Go rewrite) |

After #214: the entire services tier is single native Go/Rust binaries. No Python
runtime, no pip wheels, no bundler fragility.

Pinned versions at design time (update in `#220` implementation):
- NATS: 2.10.18
- SurrealDB: 3.2.0
- engram: 1.16.1
- webterm-gateway: built from current pinard commit

## Swift `ServiceOrchestrator` Lifecycle Model

### Binary embedding

Each service binary is placed in `Contents/Helpers/<service>` (or
`Contents/MacOS/<service>` for the main executable). All embedded binaries are
Universal (lipo of arm64 + amd64) or at minimum ship one slice that matches the
running arch. The app's `Info.plist` declares them under `CFBundleExecutable` companions
so Gatekeeper quarantine is handled at app-level notarization time.

### Startup (dependency-ordered, health-gated)

Services start in the order established by the Docker s6-overlay reference:

```
1. NATS          (no dependencies)
2. engram        (depends: NATS up)
3. SurrealDB     (depends: NATS up)
4. memory        (depends: engram + SurrealDB healthy)
5. webterm-gateway (depends: NATS + memory healthy)
```

Each step:
1. `Process.launch()` the binary with its env/args/working-dir.
2. Probe the health endpoint (HTTP `/healthz` or TCP connect) with exponential backoff
   (max 30 s). On timeout → log error + surface diagnostic; abort remaining starts.
3. Proceed to the next dependency tier.

### Teardown (SIGTERM → SIGKILL, no zombies)

On app quit, crash, or explicit `pinard down`:
1. Send `SIGTERM` to all supervised children in reverse start order.
2. Wait up to 10 s for each to exit (`waitpid`).
3. Any process still alive after the grace period receives `SIGKILL`.
4. All `Process` handles are released; no zombie processes remain.

`kqueue`/`NSTask.terminationHandler` catches unexpected child exits and logs them to the
diagnostic panel; the orchestrator attempts a restart with back-off (max 3 attempts
before giving up and surfacing an alert).

### Loopback ports

All services bind to `127.0.0.1` only — no external interface exposure in the default
solo configuration.

| Service | Default port |
|---------|-------------|
| NATS | 4222 |
| engram | 7437 |
| SurrealDB | 8000 |
| webterm-gateway | 8080 |
| memory (gRPC/HTTP) | 9090 |

Ports are configurable via the app's preferences UI and persist to
`~/Library/Application Support/Pinard/config.json`. The `solo` profile written by
`aoc init --local` reads these values (or uses the defaults).

### Unified log piping and diagnostics

Each child's `stdout` and `stderr` are piped into the orchestrator via `Pipe`. Lines
are:
- Prefixed with the service name and written to
  `~/Library/Application Support/Pinard/logs/<service>.log` (rotating, max 10 MB).
- Streamed to the in-app **diagnostic panel** (a SwiftUI sheet) on demand.
- Searchable and filterable by service and log level.

### Launch Agent integration (`SMAppService`)

The app registers itself as a `SMAppService` Launch Agent so it can be configured to
start at login. The Launch Agent starts the orchestrator (which starts services) before
the user opens the menu-bar icon. Status (running / stopped / error) is reflected in
the menu-bar icon badge.

## Code Signing and Notarization

All binaries embedded in `Contents/Helpers/` must be **individually signed** with a
Developer ID Application certificate before the outer `.app` is signed and notarized.
Steps:

1. Download each third-party binary (NATS, engram, SurrealDB) from their release pages
   or build from source in CI.
2. Run `codesign --deep --force --verify --verbose --sign "Developer ID Application: …"
   --options runtime <binary>` for each binary.
3. Lipo arm64 + amd64 slices into Universal binaries where available.
4. Build the Swift app, sign the `.app` bundle (`codesign --deep`).
5. Create a `.dmg` or `.pkg`, submit for notarization via `notarytool`, staple the
   ticket.
6. Hardened Runtime (`--options runtime`) is required for notarization; all embedded
   Go/Rust binaries support it.
7. After #214 the memory Go binary follows the same signing path. Python wheels with
   native extensions would have required per-wheel signing — this was the primary
   motivation for the Go rewrite.

## Data and Volume Layout

All persistent state lives under the app's Application Support directory. The path is
stable across app updates.

```
~/Library/Application Support/Pinard/
  config.json          — port overrides, feature flags
  logs/
    nats.log
    engram.log
    surrealdb.log
    memory.log
    webterm-gateway.log
  data/
    nats/              — JetStream file store (KV, streams)
    engram/            — engram.db (SQLite)
    surrealdb/         — SurrealDB file backend
    memory/            — memory layer state (post #214 Go binary)
```

Data directories are created by the orchestrator on first launch with `0700`
permissions. The `solo` profile (`aoc init --local`) points its `engram.data_dir` and
NATS `store_dir` config to these paths.

### Migration from Docker volume

When a user migrates from the Docker solo path to the native app, they can copy the
volume contents into the Application Support paths. The orchestrator does not automate
this migration in v1.

## Risks and Trade-offs

| Risk | Mitigation |
|------|-----------|
| #214 Go memory rewrite delayed → native path blocked | #214 is tracked as hard prerequisite; the Docker path (#212) remains the functional solo fallback until it lands |
| macOS update breaks notarization or binary quarantine | Pin signing tools in CI; test on fresh VMs each release; notarise every release |
| Port conflicts on developer machines | Port configurability in `config.json`; `aoc init --local` reads the configured ports |
| Child process crash loops | Back-off + max-restart policy (3 attempts); surface alert + link to logs; no silent restart |
| Universal binary bloat | Accept: NATS arm64 ≈ 18 MB, amd64 ≈ 20 MB; total services tier ≈ 150 MB signed — reasonable for a native app |
