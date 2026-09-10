## Context

tmux sessions (vendangeurs as flat sessions; the régisseur + maîtres as windows of the `conductor` session) live on the **pinard host**, not in k8s. Reviewing them today requires SSH + `tmux attach`. We want a browser link — postable on a GitLab issue/MR — that opens a **read-only** live view of a session, plus a way to navigate the control room, all behind corp SSO. `internal/session` already has the tmux helpers (`list-sessions`, `list-windows`, sanitize, attach).

## Goals / Non-Goals

**Goals:**
- Browser, read-only-by-default live view of a specified tmux target, authenticated by SSO and authorized per session.
- Vendangeur-posted signed links on issues/MRs scoped to one session.
- Navigate the control room (régisseur, maîtres, vendangeurs) from an authed index.
- Opt-in writable "steer" behind a higher-privilege role.
- On-demand spawn + automatic teardown; small attack surface; audit trail.

**Non-Goals:**
- Replacing tmux/SSH for operators who have host access.
- A full collaborative multi-cursor editor; this is view-first (+ optional single-writer steer).

**In scope via NATS (was previously a non-goal):**
- **Remote/HPC (standalone) vendangeurs** — because their host is a NATS client, running the terminal-responder alongside them makes their sessions viewable through the same gateway.

## Decisions

### D1 — Split: k8s gateway + per-host terminal responder, bridged over NATS
The endpoint lives in k8s (`pinard.example.com/sessions`) but tmux lives on the pinard host (and HPC nodes) — off-cluster. Rather than expose the host inbound, **reuse NATS** (every host already dials out to it) as the transport:

```
browser ⇄ WebSocket ⇄ gateway (k8s) ⇄ NATS ⇄ terminal-responder (tmux host) ⇄ tmux attach -r
```

- **Gateway (k8s, Go):** serves the xterm.js frontend + the control-room index; authenticates (D2), authorizes per target (D3-authz), audits; bridges the browser WebSocket to per-session NATS subjects. Deployable like engram/website.
- **Terminal-responder (Go, on each tmux host / standalone worker):** subscribes to terminal-request subjects, verifies the gateway's signed grant (below), runs `tmux attach -r -t <target>` via an embedded PTY (`creack/pty`), streams PTY frames back over NATS; a separate subject carries input for writable mode. Reuses `internal/session` helpers.
- **PTY over NATS:** real-time **core NATS (never JetStream** — persisting every terminal byte would be the actual mistake), a unique subject pair per viewer session (like BTW/interrupt), binary frames; resize as a control message. Latency = one internal NATS hop. Throughput is a non-issue for NATS (millions of small msgs/s); the guards that matter are **flow control** — responder-side rate-cap + frame coalescing + bounded buffer so a runaway `cat`/`yes` can't flood a viewer — **concurrency limits** on live viewers, and a **dedicated NATS account/connection** for terminal traffic so bursts never starve the control plane (events/inboxes).
- **Host-side trust:** the responder MUST NOT attach on any request — anyone with NATS creds could publish. The gateway includes a **short-lived HMAC grant** (shared secret) encoding target + mode + expiry; the responder verifies it. The gateway remains the sole SSO/authz boundary.
- *Alternatives rejected:* a gateway ON the host exposed via Traefik ExternalName/Endpoints (needs cluster→host inbound reachability; NATS avoids it) — and sshx/tmate relays (extra relay service / own tmux server; NATS already IS our relay). ttyd remains a possible responder-side PTY backend but embedding avoids bundling a binary.

### D2 — Cognito authentication; authorization by identity-match (no custom claims)
Auth is corp Cognito (SAML-federated the platform identity; ID tokens like the sample). **oauth2-proxy** (or Traefik forward-auth) runs the Cognito OIDC code flow for the browser and injects identity to the gateway, which validates the JWT against the pool JWKS (`iss` `…<region>_<pool-id>`, `aud`, `exp`, `token_use: id`).

The `groups` claim is **AD-sourced and not pinard-controllable**, so it is **not** used for pinard authorization. At most, oauth2-proxy may use a broad AD group as a coarse "allowed into the app at all" gate. Fine-grained authorization keys off the **identity** that is already in every token — `email` / `preferred_username` — never a custom claim.

Authorization per tmux target:
- **Viewer (read-only, one session):** a valid signed link (D4) + authenticated. No mapping needed.
- **Operator (control room + writable eligibility):** the JWT `email`/`preferred_username` matches the **vignoble's owner**, discovered dynamically (D7) — not a hand-maintained list.
Every decision is logged with the identity.

- *Alternative — signed URLs only (no SSO):* simpler but any holder of an unexpired link gets in; rejected as the primary gate (kept as the scoping layer on top of Cognito).

### D7 — Operator discovered from the vignoble's own credentials (no manual mapping), matched by username
The vignoble owner is the **`nats.user`** in that vignoble's `credentials.yaml` (e.g. `example-user`) — the human tenant, optionally overridden by an explicit `owner:` field. The responder/daemon publishes `vignoble → owner username` to a KV entry; the gateway grants operator role when the JWT **`preferred_username`** matches. Self-maintaining (tracks `credentials.yaml`, no separate config).

**Match on username, not email.** Confirmed against a real token + host: `git config --global user.email` (`example-user@example.com`) and the JWT `email` (`lelong.sebastien@example.com`) are **divergent aliases**, and `credentials.yaml` `git_email` is the *bot* (`ssf.pinard@robots.example.com`). The stable join key is the username `example-user`, identical across the JWT `preferred_username`/`identities.userId`, `nats.user`, and the git-email local part.

- *Alternatives rejected:* email-match (aliases diverge; `git_email` is the service account); a static identity→role config (the manual upkeep to avoid).

### D3 — Read-only default; non-disruptive navigation via grouped sessions
Default views attach read-only (`tmux attach -r`), so a viewer cannot type or hijack. Writable steer requires the elevated claim and is a distinct request. To let a viewer navigate to a *specific* control-room window without yanking the operator's active window, create a per-viewer **grouped session** (`tmux new-session -t conductor`) pinned to the target window and attach read-only to that — the real operator is unaffected. (v1 may attach read-only to the live session showing the active window; grouped-session pinning is the v2 refinement.)

### D4 — Signed, expiring, session-scoped links
The vendangeur builds `…/term?target=<parcelle:session>&exp=<ts>&sig=<hmac>` using a shared signing secret (`credentials.yaml`/Vault) and posts it on the MR/issue (next to the existing "Vendangeur attached" comment). The gateway verifies signature + expiry + scope, then still requires SSO. Short TTL (minutes–hours). This is the "secret URL" pattern already used elsewhere.

### D5 — On-demand lifecycle
No always-on terminals. A viewer is spawned per request, torn down on disconnect/idle and when the tmux target exits. Concurrency and idle timeouts are bounded to cap resource use and exposure.

### D6 — Exposure: `/sessions` path on the existing internal host
Expose the gateway as a Traefik `PathPrefix(`/sessions`)` rule on `Host(pinard.example.com)` (the existing website host, `web` entrypoint, TLS terminated at the edge — same pattern as pinard-website/engram). That host resolves to a **private IP (internal-only)**, limiting network exposure; Cognito SSO is still required regardless. Because the gateway reaches tmux over NATS (D1), no inbound path to the tmux host is needed.

## Risks / Trade-offs

- **Terminals expose sensitive work / control** → read-only default; writable gated on elevated role; SSO + scoped links; audit.
- **Writable session hijack** → single-writer, elevated-claim only, explicit request, audited; consider requiring re-auth for writable.
- **Token/link leakage** (links land in GitLab comments) → SSO still required even with a valid link; short TTL; session-scoped; revocable via secret rotation.
- **Raw PTY exposed** → backend binds loopback, only via the gateway.
- **tmux read-only can't change windows** → grouped per-viewer sessions (D3); v1 limited to the active window.
- **Remote/HPC vendangeurs unreachable** → out of scope for v1; would need a gateway/relay on that host.
- **Bundling ttyd** (if chosen) → extra artifact in `dist`; embedding PTY avoids it.

## Migration Plan

- **Phase 1 — interim, SSO deferred.** Gateway + read-only attach to a **single vendangeur session** via signed link (the MR-link use case), over NATS. **No Cognito client yet**, so the **signed expiring link + internal-only (private-IP) network is the sole gate**; keep TTLs short and read-only strict. Writable steer and the control-room index are **held** until SSO lands (they need a verified identity, not just link possession). This is a documented, temporary deviation from the SSO requirement — acceptable because the surface is internal + read-only + short-lived links.
- **Phase 2 — add Cognito SSO** in front (oauth2-proxy) once the client/app is provisioned; enforce the authentication + operator-discovery requirements.
- **Phase 3 — control-room navigation index** (régisseur/maîtres/vendangeurs) for operator-role users (requires Phase 2).
- **Phase 4 — opt-in writable steer** (operator identity) + grouped-session window navigation (requires Phase 2).

Each phase is independently shippable; rollback = disable the gateway route (links simply stop resolving; tmux/SSH unaffected).

## Open Questions

- **PTY-over-NATS framing:** exact subject scheme + backpressure params (max message size, coalescing threshold), and one shared responder per host vs one per session.
- **Writable steer:** still worth building given NATS `send_message`/BTW already steer agents? Default plan: read-only first, writable behind operator identity later.
- **oauth2-proxy vs in-gateway OIDC** for the Cognito code flow (both viable; oauth2-proxy is less code).
- **Coarse app gate:** should oauth2-proxy require a broad AD group (e.g. `EXOHUB_EDS_HUGE_USERS`) to reach the app at all, on top of per-session authz?

*Resolved from discussion:* exposure = `/sessions` on `pinard.example.com` (internal); auth = Cognito ID token; **authz by identity-match, not `groups`** (AD-sourced/uncontrollable); **operator auto-discovered from `credentials.yaml` `nats.user` → KV, matched on JWT `preferred_username`** (username, not email — aliases diverge; no manual mapping); transport = core NATS (no host inbound, with flow-control guards); remote/HPC vendangeurs reachable via the NATS responder.
