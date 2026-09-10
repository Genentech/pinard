## Why

**Pinard today has one "solo" deployment artifact**: a Docker image (`pinard-services`,
#212 / MR !411) bundling NATS, engram, SurrealDB, and the memory layer under s6-overlay
supervision. On **macOS** this means: install Docker Desktop, spin up a Linux VM, start
the image. That is three layers of friction before an agent runs a single command.

**The macOS friction problem.** Docker Desktop requires explicit installation, ~4 GB
disk, a hypervisor-backed Linux VM, and elevated permissions — all to serve four
service binaries that already exist as native darwin binaries (NATS, engram,
SurrealDB, webterm-gateway are all Go or Rust). The overhead is disproportionate for a
single-user laptop deployment.

**The App Sandbox incompatibility.** A natural alternative — wrap the entire Pinard
stack (agents included) in a macOS app with a hard App Sandbox — is incompatible with
the core use case. A coding-agent tool must read and write the user's real repository
trees, invoke arbitrary dev tooling (compilers, formatters, test runners), and shell
out freely. The hard App Sandbox confines file I/O to the container path and forbids
arbitrary subprocess execution. Any sandboxing must therefore apply **only** to the
infra service tier, never to the agent runtime.

**The result**: instead of forcing one Docker image to serve both macOS laptops and
Linux/CI environments, we promote solo deployment into **two first-class tiers** —
each optimised for its host.

## What Changes

### Two first-class deployment tiers

**Tier 1 — Enterprise (Kubernetes, unchanged)**

The existing `charts/pinard` + `charts/pinard-nats` multi-pod deployment. Full scale,
SSO (Cognito), Vault/ESO, LLM proxy, cloud Engram/RDS. No change from today.

**Tier 2 — Solo (macOS-native services + agents in shell)**

A native **menu-bar / launch-agent macOS app** supervises the *services tier* as
native child processes over `127.0.0.1` — **no Docker, no Linux VM**.

**Design (a) — services-only app, agents in the user's shell (the load-bearing
decision):**
- The app supervises **only** the service binaries: NATS, engram, SurrealDB,
  webterm-gateway. After #214 the Go memory binary joins this set.
- The **agent runtime** (`aoc`, conductor, workers = `pi`, babysitter, git, compilers)
  runs in the user's **normal shell, unsandboxed**, and connects to the services on
  `127.0.0.1` — exactly as `aoc` connects to the cluster today; only the endpoints
  change.
- **Sandbox rationale**: no hard App Sandbox on agents (see above). An optional
  per-service sandbox (entitlements-restricted) applies only to the infra processes and
  is separate from the agent runtime.

**Rejected design (b) — full App Sandbox hosting everything**: incompatible with
arbitrary file I/O and subprocess execution required by the agent runtime. Formally
rejected; see design.md.

### Docker image repurposed as Linux/dev/CI fallback

The `pinard-services` Docker image (#212 / MR !411) is **retained** but
**repurposed**: it is now the **Linux / dev-in-container / CI** solo backend and a
portable fallback — not the primary macOS path. Its s6-overlay supervision dependency
graph (`nats → engram + surreal → memory → webterm-gateway`, health-gated) is the
reference the Swift `ServiceOrchestrator` mirrors.

### Go rewrite of `services/memory` (#214) is a hard prerequisite

The memory layer (`services/memory/*`) is Python. On the native macOS path the services
tier must ship as native darwin binaries the `ServiceOrchestrator` can `Process.run`.
Shipping the memory layer natively would require py2app/PyInstaller bundling of a
Python 3.11 runtime + per-arch native wheels + code signing + notarization of the
whole bundle — a fragile, maintenance-heavy path. #214 rewrites memory in Go (single
binary), removing the only real packaging blocker. Once memory is Go, the entire
services tier is native Go/Rust binaries.

### `solo` profile is endpoint-agnostic

The `aoc init --local` `solo` profile (written by #213) specifies identical localhost
endpoints (`nats://127.0.0.1:4222`, `ws://127.0.0.1:8000`, …) whether served by the
native app (macOS) or the Docker container (Linux). `aoc` **connects only** — it never
supervises services. The profile does not care which backend brought them up.

## Capabilities

### New Capabilities

- `solo-native-services`: The native macOS services app — Swift `ServiceOrchestrator`,
  service binary embedding, dependency-ordered startup, health gating, teardown,
  unified logging, diagnostics, data layout under Application Support, code signing +
  notarization.

### Modified Capabilities

- `aoc-solo-profile` (tracked in #213): `aoc init --local` produces endpoint-agnostic
  `solo` profile credentials; profile is now backend-aware (macOS native or Linux
  Docker); no change to endpoints themselves.
- `pinard-up-down` (tracked in #215): `pinard up` / `pinard down` drive the native
  `ServiceOrchestrator` on macOS (start/stop native child processes) and
  `docker run`/`stop` on Linux. Backend selection is automatic (OS detection). Both
  expose the same localhost endpoints.

## Impact

- **Packaging and distribution** (`#220`): macOS `.app` bundle with embedded darwin
  binaries (`Contents/Helpers/`), `Info.plist`, `SMAppService` / `launchd` integration,
  code signing and notarization (all embedded binaries must be signed; memory Go binary
  is the last piece after #214), Sparkle auto-update or direct download.
- **`aoc init` / solo profile** (`#213`): BYO LLM key (pi direct provider), no cloud
  services, no proxy, local `.env` secrets. Profile identical across both solo backends.
- **`pinard up` / `pinard down`** (`#215`): backend-aware CLI command; macOS = drive
  the native app; Linux = `docker run pinard-services`; no-auth local webterm in both.
- **Docs** (`#216`): "Full Pinard on your Mac in 5 minutes" — install the native app
  → `aoc init --local` → `pinard up` → BYO key. Docker path documented as the Linux
  fallback. Enterprise k8s path unchanged.
- **Enterprise** (`charts/`): zero impact. This change does not touch k8s charts.
