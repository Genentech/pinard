## ADDED Requirements

### Requirement: Browser Terminal Gateway

The system SHALL provide a gateway (reachable in-cluster at `pinard.example.com/sessions`) that streams a live tmux target (session or window) to a browser via a terminal emulator over a WebSocket. The gateway bridges the browser WebSocket to a host-side terminal responder over NATS; it MUST NOT require any inbound network path to the tmux host, and the raw PTY is never exposed to the network.

#### Scenario: View a live session
- **WHEN** an authorized user opens a terminal link for tmux target `T`
- **THEN** the gateway bridges the browser to the responder for `T` over NATS and streams `T`'s live output to the browser terminal
- **AND** terminal resize events from the browser are applied to the PTY

#### Scenario: No inbound to the tmux host
- **WHEN** the gateway (in k8s) connects a viewer to a tmux target on the pinard host or an HPC node
- **THEN** it uses NATS (an outbound connection the host already holds) and requires no inbound port on that host

#### Scenario: Target does not exist
- **WHEN** a link references a tmux target that is not running (no responder answers / responder reports absent)
- **THEN** the gateway returns a clear "session not found / ended" response instead of attaching

### Requirement: Host-Side Terminal Responder

Each tmux host (the pinard host and standalone/HPC worker hosts) SHALL run a terminal responder that attaches to local tmux targets on request and streams the PTY over NATS. The responder MUST verify a gateway-issued authorization grant before attaching and MUST NOT act on unauthenticated requests.

#### Scenario: Responder attaches read-only and streams
- **WHEN** the responder receives a request carrying a valid gateway grant for target `T` in read-only mode
- **THEN** it attaches read-only (`tmux attach -r -t T`) and streams PTY frames back over the session's NATS subject

#### Scenario: Reject a request without a valid grant
- **WHEN** a NATS message requests a terminal for `T` without a valid, unexpired gateway grant (e.g. published directly by another NATS client)
- **THEN** the responder refuses to attach

#### Scenario: Remote/HPC vendangeur reachable
- **WHEN** a standalone (HPC) vendangeur runs the responder and is connected to NATS
- **THEN** its tmux session is viewable through the same gateway, with no host-specific inbound path

#### Scenario: Runaway output is flow-controlled, not flooded
- **WHEN** a viewed session emits a high-rate burst (e.g. `cat` of a large file)
- **THEN** the responder rate-caps/coalesces frames within a bounded buffer over **core NATS (not JetStream)**
- **AND** terminal traffic uses a dedicated account/connection so it cannot starve the control-plane subjects

### Requirement: On-Demand Session Lifecycle

Terminal viewers SHALL be created on demand for a specific target and torn down automatically. The system MUST NOT keep an always-on terminal per session.

#### Scenario: Spawn on request
- **WHEN** an authorized request for target `T` arrives and no viewer is running for it
- **THEN** the gateway spawns a read-only attach to `T` for that request

#### Scenario: Teardown on disconnect/idle
- **WHEN** the browser disconnects or the viewer is idle past the configured timeout
- **THEN** the gateway terminates that viewer and frees its resources

#### Scenario: Teardown when the session ends
- **WHEN** the underlying tmux target exits while being viewed
- **THEN** the gateway closes the WebSocket and does not leave an orphaned backend process

### Requirement: SSO/JWT Authentication

Access to any terminal or the navigation index SHALL require a valid authenticated identity via corp SSO. The gateway MUST validate the JWT and reject expired/invalid/untrusted tokens.

#### Scenario: Unauthenticated request
- **WHEN** a request arrives with no valid SSO session / JWT
- **THEN** the gateway denies access (redirect to SSO or 401) and streams nothing

#### Scenario: Identity from claims
- **WHEN** a request carries a valid Cognito ID token (validated against the pool JWKS: `iss`, `aud`, `exp`, `token_use: id`)
- **THEN** the gateway derives the user identity (`preferred_username` / `email`) and entitlements from it for authorization and audit

#### Scenario: Expired token
- **WHEN** the JWT is expired
- **THEN** access is denied even if the link/URL is otherwise well-formed

### Requirement: Per-Session Authorization

Authorization SHALL be decided per tmux target from the authenticated identity and claims. A signed link grants access only to its scoped target; broader access requires an operator/owner role.

#### Scenario: Scoped link grants one session
- **WHEN** an authenticated user opens a valid signed link scoped to session `S`
- **THEN** they may view `S`
- **AND** they may not view any other session by editing the target unless separately authorized

#### Scenario: Authorization does not depend on AD/custom claims
- **WHEN** deciding operator/viewer authorization
- **THEN** the gateway uses only the identity claims present in a standard token (`email`/`preferred_username`) and MUST NOT depend on the `groups` claim (AD-sourced, not pinard-controllable) for per-vignoble authorization

#### Scenario: Operator matches the vignoble owner
- **WHEN** an authenticated user's `preferred_username` matches the vignoble's owner username (discovered per the Operator Discovery requirement)
- **THEN** they get the operator role: view the régisseur, maître, and vendangeur sessions, the navigation index, and writable eligibility

#### Scenario: Non-entitled user denied
- **WHEN** an authenticated user has neither a valid scoped link nor a role entitling them to the requested target
- **THEN** access is denied

### Requirement: Operator Discovery

The vignoble owner SHALL be discovered dynamically from the vignoble's own `credentials.yaml` — the **`nats.user`** username (optionally overridden by an explicit `owner:` field) — not from a hand-maintained mapping. The owner username MUST be made available to the gateway (e.g. published to a KV entry by the host responder/daemon). Authorization matches on **username**, not email.

#### Scenario: Owner sourced from credentials
- **WHEN** a host publishes its vignoble's presence
- **THEN** it includes the owner username read from that vignoble's `credentials.yaml` (`nats.user`, or the `owner:` override)

#### Scenario: Match on username, not email
- **WHEN** authorizing an operator
- **THEN** the gateway compares the JWT `preferred_username` to the owner username
- **AND** it does NOT rely on email (the human's SSO email and git email are divergent aliases, and the credential `git_email` is the service account)

#### Scenario: Mapping tracks credential changes
- **WHEN** a vignoble's `credentials.yaml` owner changes
- **THEN** the operator authorization tracks the new value without any separate manual configuration

### Requirement: Read-Only by Default with Opt-In Writable

Terminal views SHALL be read-only by default; input (writable "steer") MUST require a higher-privilege claim/role and be explicitly requested.

#### Scenario: Default view is read-only
- **WHEN** a user opens a terminal without writable entitlement
- **THEN** output streams to the browser and keyboard input is not delivered to the session

#### Scenario: Writable requires elevation
- **WHEN** a user requests a writable session
- **THEN** the gateway grants input only if the identity carries the writable/steer entitlement, else it falls back to read-only

#### Scenario: Read-only viewer does not disturb the operator
- **WHEN** a read-only viewer navigates or attaches
- **THEN** the interactive operator attached to the same tmux target is not forced to a different window or interrupted

### Requirement: Signed Session Links

A vendangeur SHALL be able to produce a signed, expiring, session-scoped URL and post it on the issue/MR it is working, so an authorized person can open the session in a browser.

#### Scenario: Link posted on the MR
- **WHEN** a vendangeur opens/updates its MR (or attaches)
- **THEN** it posts a link to its own session scoped to that session

#### Scenario: Signature validated
- **WHEN** the gateway receives a signed link
- **THEN** it verifies the signature, expiry, and target scope before granting the (still SSO-authenticated) view

#### Scenario: Tampered or expired link rejected
- **WHEN** a link's signature is invalid or its expiry has passed
- **THEN** the gateway denies access

### Requirement: Control-Room Navigation

The gateway SHALL present an authenticated index of the tmux sessions/windows the user is entitled to — régisseur, each maître, and each vendangeur — as links to their terminal views.

#### Scenario: Index lists entitled sessions
- **WHEN** an authorized operator opens the gateway index
- **THEN** it lists the régisseur window, the maître windows, and the vendangeur sessions with a link to each
- **AND** sessions the user is not entitled to are not shown

#### Scenario: Navigate to a control-room pane
- **WHEN** the operator clicks the régisseur or a maître entry
- **THEN** a read-only terminal view of that window opens

### Requirement: Access Audit Log

The gateway SHALL record an audit entry for each terminal access.

#### Scenario: Access recorded
- **WHEN** a user opens a terminal view
- **THEN** an audit entry records the identity, the target session, the timestamp, and whether the view was read-only or writable
