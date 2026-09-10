## Purpose

Remote dispatch lets a vigne's workers be spawned on a different compute host than
the daemon, reached over SSH-tunneled tmux and WebSocket NATS, so heavy or
location-bound workloads run where the data and resources are.
## Requirements
### Requirement: Compute Host Configuration

Remote dispatch SHALL be configured via an optional `compute_host` field per vigne in vignes.yaml.

#### Scenario: Local dispatch (no compute_host)
- **WHEN** a vigne has no `compute_host` field
- **THEN** workers are spawned on the local tmux server (default behavior)

#### Scenario: Remote dispatch configured
- **WHEN** a vigne has `compute_host: "workbox"` (an SSH host alias or address)
- **THEN** workers for that vigne are spawned on the remote tmux server at that host

---

### Requirement: SSH Tunnel Management

Remote dispatch SHALL use SSH tunneling to forward the remote tmux socket locally.

#### Scenario: Establish tunnel
- **WHEN** a spawn targets a remote compute host
- **THEN** the daemon opens an SSH tunnel: `ssh -L <local-socket>:<remote-tmux-socket> <host> -N`
- **AND** the daemon connects to the local forwarded socket

#### Scenario: Tunnel reuse
- **WHEN** multiple spawns target the same remote host
- **THEN** the same SSH tunnel is reused (not duplicated)

#### Scenario: Tunnel failure
- **WHEN** SSH tunnel cannot be established (host unreachable, auth failure)
- **THEN** the spawn command fails with error: "cannot reach compute host '<host>': <ssh error>"

#### Scenario: Tunnel cleanup
- **WHEN** the daemon shuts down
- **THEN** all SSH tunnels are closed

---

### Requirement: Remote Worker NATS Connectivity

Remote workers SHALL connect to NATS over WebSocket, same as local workers.

#### Scenario: NATS URL passed to remote worker
- **WHEN** a worker is spawned on a remote host
- **THEN** the worker command includes `PINARD_NATS_URL` pointing to the NATS server (accessible from the remote host)
- **AND** the worker connects via WebSocket transport

#### Scenario: NATS unreachable from remote
- **WHEN** the remote host cannot reach the NATS server
- **THEN** the worker fails to start and publishes no KV state
- **AND** the daemon detects the worker as dead on next `sessionIsAlive` check

---

### Requirement: Remote Worktree Setup

Remote workers SHALL have their git worktree on the remote host.

#### Scenario: Remote project path
- **WHEN** spawning on a remote host
- **THEN** the project path on the remote host is resolved (same relative path or configured `remote_path` per vigne)
- **AND** git fetch + worktree creation happens on the remote host via SSH commands before tmux session creation

#### Scenario: Remote host prerequisites
- **WHEN** remote dispatch is configured
- **THEN** the remote host MUST have: tmux running, git access to the repo, SSH key for GitLab, and network access to NATS

### Requirement: Sandboxed worker launch
The singularity launcher SHALL run sandboxed workers with
`singularity run --containall --writable-tmpfs <binds> <sif>` and SHALL pass the
bootstrap environment (`PINARD_UNCORK_URL`, `PINARD_POUR_URL`) through to the
container when set. `--writable-tmpfs` ensures the rootfs overlay (where
bootstrapped secrets are written) is RAM-backed and ephemeral.

#### Scenario: Launch a sandboxed worker with bootstrap env
- **WHEN** the launcher spawns a `runtime: singularity` worker with
  `PINARD_UNCORK_URL` / `PINARD_POUR_URL` in the environment
- **THEN** the `singularity run` invocation includes `--containall --writable-tmpfs`
- **AND** the bootstrap env vars are available inside the container

#### Scenario: Launch without bootstrap env
- **WHEN** the launcher spawns a sandboxed worker with no bootstrap URLs set
- **THEN** the worker still launches and relies on bind-mounted credentials

