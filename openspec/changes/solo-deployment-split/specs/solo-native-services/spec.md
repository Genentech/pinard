## ADDED Requirements

### Requirement: Native macOS Service Orchestration

On macOS the Pinard services tier (NATS, engram, SurrealDB, memory, webterm-gateway)
SHALL run as **native child processes** of a macOS app (`ServiceOrchestrator`), with no
Docker daemon or Linux VM dependency.

#### Scenario: Services start without Docker
- **GIVEN** a macOS machine with the Pinard native app installed and no Docker Desktop
- **WHEN** the user runs `pinard up` or starts the app
- **THEN** NATS, engram, SurrealDB, memory, and webterm-gateway start as native
  darwin processes on `127.0.0.1`
- **AND** no Docker daemon, container, or Linux VM is involved

#### Scenario: Services stop cleanly on app quit
- **WHEN** the user quits the Pinard app or runs `pinard down`
- **THEN** each supervised service receives SIGTERM in reverse start order
- **AND** any service still running after a 10-second grace period receives SIGKILL
- **AND** no zombie processes remain after teardown

#### Scenario: Unexpected child exit triggers restart with back-off
- **WHEN** a supervised service exits unexpectedly
- **THEN** the orchestrator restarts it with exponential back-off
- **AND** after 3 failed restart attempts, the orchestrator surfaces an alert and stops
  retrying that service
- **AND** the failure is recorded in the service's log file

### Requirement: Dependency-Ordered Startup with Health Gating

The orchestrator SHALL start services in dependency order and SHALL NOT proceed to the
next tier until the current tier's services pass their health check.

#### Scenario: NATS must be healthy before dependent services start
- **WHEN** `pinard up` is invoked
- **THEN** NATS starts first and its health is confirmed (TCP connect to port 4222)
  before engram or SurrealDB are launched

#### Scenario: Memory service waits for engram and SurrealDB
- **WHEN** NATS, engram, and SurrealDB are all healthy
- **THEN** the memory service is launched
- **AND** webterm-gateway is launched only after memory is healthy

#### Scenario: Health-check timeout surfaces a diagnostic
- **WHEN** a service does not pass its health check within 30 seconds
- **THEN** startup of that service (and its dependents) is aborted
- **AND** an error message referencing the service's log file is surfaced to the user

### Requirement: Agent Runtime Runs Unsandboxed in the User's Shell

The Pinard agent runtime (`aoc`, conductor, workers) SHALL run in the **user's normal
shell** with full filesystem access. It SHALL NOT be managed or sandboxed by the
macOS app.

#### Scenario: Worker reads a real repository
- **GIVEN** a worker spawned by the conductor to work on a repository in `~/projects/`
- **WHEN** the worker calls `read_file` or runs `git` commands
- **THEN** it can read and write files in `~/projects/` without restriction
- **AND** this access is not mediated by the macOS app

#### Scenario: Agent runtime connects to services on 127.0.0.1
- **WHEN** `aoc` or a worker initialises its NATS connection
- **THEN** it connects to `nats://127.0.0.1:4222` (or the configured port)
- **AND** this is functionally identical to connecting to the enterprise cluster
  (only the endpoint differs)

#### Scenario: No agents inside the app bundle
- **GIVEN** the Pinard macOS app is running
- **THEN** the app process tree contains only the five service binaries
- **AND** `aoc`, `pi`, babysitter, git, and compilers are NOT child processes of the app

### Requirement: Loopback-Only Service Binding

All services managed by the `ServiceOrchestrator` SHALL bind exclusively to the
loopback interface (`127.0.0.1`) and SHALL NOT listen on external network interfaces
in the default solo configuration.

#### Scenario: Services not reachable from the network
- **GIVEN** the Pinard native app is running on a laptop connected to a network
- **WHEN** a remote host attempts to connect to port 4222 on the laptop's IP
- **THEN** the connection is refused (NATS is not bound to that interface)

#### Scenario: Local agent connects without network
- **GIVEN** the services are running on 127.0.0.1
- **WHEN** the user runs `aoc` on the same machine
- **THEN** it connects successfully via the loopback interface

### Requirement: Persistent State Under Application Support

All service data SHALL be stored under
`~/Library/Application Support/Pinard/data/<service>/` and SHALL persist across app
restarts, updates, and OS reboots.

#### Scenario: JetStream data survives app restart
- **GIVEN** the Pinard app has written NATS JetStream messages and KV entries
- **WHEN** the app is quit and relaunched
- **THEN** all JetStream data and KV entries are present in NATS
- **AND** no messages are lost

#### Scenario: Data directory created on first launch
- **GIVEN** a fresh installation with no prior data
- **WHEN** the app is launched for the first time
- **THEN** `~/Library/Application Support/Pinard/data/` and all service subdirectories
  are created with `0700` permissions before any service is started

### Requirement: Unified Log Piping and Diagnostics

The orchestrator SHALL capture each service's stdout and stderr and make them available
in rotating log files and via an in-app diagnostic panel.

#### Scenario: Log file per service
- **WHEN** a service writes to stdout or stderr
- **THEN** those lines are written to
  `~/Library/Application Support/Pinard/logs/<service>.log`
- **AND** log files rotate at 10 MB with at least one prior rotation kept

#### Scenario: Diagnostic panel shows live output
- **WHEN** the user opens the diagnostic panel from the menu-bar icon
- **THEN** they see the recent log output for each service
- **AND** they can filter by service name

### Requirement: Code Signing and Notarization

Every binary embedded in the macOS app bundle SHALL be individually code-signed with a
Developer ID Application certificate. The app bundle SHALL be notarized by Apple's
notarization service before distribution.

#### Scenario: App installs without Gatekeeper warning
- **GIVEN** a user downloads the `.dmg` on a clean Mac with default security settings
- **WHEN** they open the `.dmg` and drag the app to Applications
- **THEN** the app opens without a Gatekeeper quarantine dialog
- **AND** no "unidentified developer" warning is shown

#### Scenario: All embedded binaries are individually signed
- **GIVEN** the released app bundle
- **WHEN** `codesign --verify --deep Contents/Helpers/<binary>` is run for each
  embedded binary
- **THEN** all checks pass with no invalid signature errors

### Requirement: Launch-at-Login via SMAppService

The app SHALL support optional launch-at-login registration via `SMAppService` so that
services are available before the user opens the menu-bar icon.

#### Scenario: Enable launch at login
- **WHEN** the user enables "Launch at login" in the app's preferences
- **THEN** the app (and thus the services) start automatically when the user logs in
- **AND** services are healthy before the user's shell profile executes

#### Scenario: Disable launch at login
- **WHEN** the user disables "Launch at login"
- **THEN** the app does not start at the next login
- **AND** previously registered `SMAppService` entries are removed

## MODIFIED Requirements

### Requirement: `pinard up` / `pinard down` Backend Selection

`pinard up` and `pinard down` SHALL behave differently based on the detected host OS.
The user-facing interface (command name, endpoint defaults, exit semantics) SHALL be
identical.

#### Scenario: `pinard up` on macOS drives the native app
- **GIVEN** the user is on macOS with the Pinard app installed
- **WHEN** they run `pinard up`
- **THEN** the native `ServiceOrchestrator` starts (or signals the running app to start
  services)
- **AND** `pinard up` exits 0 when all five services pass their health checks

#### Scenario: `pinard up` on Linux drives Docker
- **GIVEN** the user is on Linux with Docker available
- **WHEN** they run `pinard up`
- **THEN** `docker run --rm -d … pinard-services` is executed with the data volume and
  env
- **AND** `pinard up` exits 0 when the container's health check passes

#### Scenario: Same localhost endpoints in both backends
- **GIVEN** either backend (native macOS or Linux Docker) has been started with
  `pinard up`
- **THEN** `aoc init --local` produces an identical `solo` profile pointing to the same
  `127.0.0.1` endpoints
- **AND** the same `aoc` binary and conductor work against both backends without
  modification

### Requirement: `aoc init --local` solo profile

`aoc init --local` SHALL produce a `solo` profile `credentials.yaml` that is
endpoint-agnostic across both solo backends.

#### Scenario: solo profile targets localhost
- **WHEN** the user runs `aoc init --local` on a machine running either solo backend
- **THEN** `credentials.yaml` contains `nats.url: nats://127.0.0.1:4222`,
  `engram: 127.0.0.1:7437`, `surrealdb: ws://127.0.0.1:8000`, and
  `webterm_gateway: http://127.0.0.1:8080`
- **AND** no cloud service URLs or cluster credentials appear in the profile

#### Scenario: BYO LLM key, no proxy
- **WHEN** a worker or conductor is started with the solo profile
- **THEN** it uses the user's own Anthropic/OpenAI API key via pi's direct provider
- **AND** no LLM proxy is involved

## Unchanged Baselines (enterprise k8s)

The following requirements are **not changed** by this proposal and are listed only to
confirm their stability:

- Enterprise deployment via `charts/pinard` + `charts/pinard-nats` is unchanged.
- Multi-pod Kubernetes topology, SSO (Cognito), Vault/ESO secrets, LLM proxy, and
  cloud Engram/RDS are unchanged.
- The NATS JetStream event model (subjects, stream topology, consumer durables) is
  unchanged; solo services use the same protocol over loopback.
- Worker lifecycle (spawn → work → MR → monitor → auto-merge → reap) is unchanged;
  only service endpoints differ.
