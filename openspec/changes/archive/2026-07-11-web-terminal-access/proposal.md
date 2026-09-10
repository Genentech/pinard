## Why

Reviewing what a vendangeur (or the régisseur/maîtres) is doing today means SSH-ing to the pinard host and attaching to the right tmux session — impractical from a phone, for a reviewer without host access, or straight from a GitLab issue/MR. A vendangeur should be able to post a **link** on the issue/MR it's working, and an authorized person should open it in a **browser** to watch that session live — with a way to navigate the whole control room (régisseur, maîtres, other vendangeurs). It must be **secure**: terminals expose work-in-progress and, if writable, control.

## What Changes

- **New browser terminal gateway** that streams a tmux session to the browser (xterm.js over WebSocket). Runs where the tmux sessions live (the pinard host).
- **On-demand, scoped sessions**: a terminal viewer is spawned per request for a specific tmux target, torn down on idle/disconnect and when the underlying session ends. No always-on per-session terminal.
- **SSO/JWT authentication + claims-based authorization**: the viewer authenticates via corp SSO; the gateway validates the JWT, identifies the user from claims, and authorizes **per session** (owner/operator role → full control room; a signed MR link → read-only view of that one session).
- **Read-only by default, opt-in writable ("steer")** gated on a higher-privilege claim/role.
- **Signed, expiring, session-scoped links** the vendangeur posts on the issue/MR (HMAC/JWT-signed URL).
- **Control-room navigation**: an authed index lists tmux sessions/windows (régisseur, each maître, each vendangeur) as links to read-only views.
- **Audit log** of who opened which session, when, and whether writable.
- Implementation direction (see design): a **custom Go gateway** embedding the PTY↔WebSocket, reusing `internal/session` tmux helpers; **ttyd** is an acceptable drop-in PTY backend.

## Capabilities

### New Capabilities
- `web-terminal-access`: Secure, browser-based, read-only-by-default viewing (and opt-in writable steering) of pinard tmux sessions — vendangeurs and the control room — via SSO/JWT-authenticated, on-demand, scoped terminal sessions, with vendangeur-posted signed links and a control-room navigation index.

### Modified Capabilities
<!-- No spec-level requirement changes to existing capabilities; link generation is added behavior implemented in the worker/mr-watcher but specified under the new capability. -->

## Impact

- **New service/binary**: the terminal gateway (Go, `internal/webterm` + an `aoc webterm` / daemon-managed process, or a standalone service), exposed with TLS behind SSO forward-auth. Reuses `internal/session` (tmux `list-sessions`/`list-windows`, read-only attach), `internal/config` (signing secret, role config).
- **Link generation**: `pi-extension/worker/` and/or `internal/watcher/mrs.go` gain the signed-URL builder; the URL is posted on the MR/issue (near the existing "Vendangeur attached" comment).
- **Auth/infra**: corp SSO/OIDC integration (forward-auth or in-gateway JWT validation); a signing secret in `credentials.yaml`/Vault; TLS termination (Traefik or the host).
- **Security surface**: terminals are sensitive — read-only default, localhost-bound PTY backend, short-TTL scoped tokens, audit logging.
- **Open question (reachability)**: whether the pinard host is directly web-reachable (gateway exposed at a hostname) or behind NAT/firewall (needs a tunnel/relay). The gateway is designed to work either way; the exposure mechanism is decided in design.
- **Specs**: new `web-terminal-access`.
