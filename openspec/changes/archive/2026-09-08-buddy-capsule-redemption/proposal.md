# Change: buddy-capsule-redemption

## Why

**Buddy Capsules** let a colleague (User B) fund a Pi agent's Claude API quota for
a specific job. The funder creates a Mnemosyne **ContractAction + CapsuleAction**
tied to a GitLab issue; the agent assigned to that issue redeems the capsule to
obtain a short-lived Claude token, does the work **on the funder's quota**,
reports usage back to the contract in real time, and posts a result when done.

Mnemosyne's server side is fully implemented (v0.20.1). This change is the
**Pinard/agent side** so a vendangeur can be funded end-to-end **without an LLM
ever driving the funding flow and without ever spending the operator's own token
on funded work**.

The mechanism is a natural extension of pinard's existing token-source
abstraction. Today a sandboxed worker gets its LLM credential from
`PINARD_POUR_URL` (a GET that returns a bearer token) resolved through
`resolveHelper()` in `pi-extension/shared/provider.ts` (mirrored by
`fetchPourToken` in `cmd/aoc/cmd_launch.go`). **A Buddy Capsule is a richer
pour:** instead of `GET → token`, redemption is an **ed25519-signed** call to
Mnemosyne. If redemption emits a bare token on stdout, it drops into the same
`apiKeyHelper`/pour slot — refresh, JWT-exp handling, and provider registration
all work unchanged.

## What Changes

- **Per-host capsule keypair (`aoc capsule-keygen` / `aoc capsule-pubkey`):**
  a one-time ed25519 keypair stored under `~/.config/pinard/` (the dir pinard
  already owns for `credentials.yaml`/`env`). The public key is shared with
  whoever creates contracts for this Pi. **Not** `~/.config/capsule/` — the key
  identifies the pinard host/tenant.

- **Deterministic redemption (`aoc capsule-redeem <contract_id>`):** all crypto
  in Go, no TS, no LLM. It: (1) probes funding + authorization via an **unsigned**
  `GET /actions/{id}/do` (`patch_url_encrypted != null` ⇔ funded; `public_key`
  must match this Pi's), (2) **signs** `GET\n/actions/{id}/do\n{ts}\n{nonce}` with
  the ed25519 key and calls the signed `do_url` to get `{token, result_key}`,
  (3) AES-GCM-decrypts the result-patch URL with `result_key`, (4) prints the
  **bare token to stdout**, and (5) stashes `{result_patch_url,
  contract_stats_url, contract_id, description}` to `<rundir>/capsule.json`.
  It is **stateless and refresh-safe**: re-invoked on every token refresh, it
  re-signs with a fresh nonce/timestamp and re-redeems; the cached lookup in
  `capsule.json` lets refreshes skip the funding probe. A **HTTP 401** on the
  signed `/do` means the proxy token expired — `capsule-redeem` re-mints once
  and retries; 410/404 → budget exhausted; other 4xx → fatal; 5xx → transient.

- **Funding gate + slow poll in the daemon:** a capsule-gated issue
  (carries `contract_id:`) is **never spawned** until its contract is funded — no
  vendangeur, no LLM, no operator-token spend. The daemon maintains a poll set
  (rebuilt on restart from GitLab labels) and slow-polls `/actions/{id}/do`
  (default ~15–30 min; contracts may sit unfunded for days). Pinard-managed
  lifecycle labels double as durable state and operator visibility:
  `capsule:awaiting-funding` → `capsule:active` → `capsule:spent` /
  `capsule:failed`. A funder-set `capsule:funded` label triggers an **immediate
  re-check** (fast-path hint); Mnemosyne `/do` remains the source of truth.

- **Token-source wiring:** `aoc capsule-redeem <id>` is wired into the existing
  `resolveHelper()` / apiKeyHelper slot (and the Go `cmd_launch.go` seed path) as
  a third token source alongside `settings.json apiKeyHelper` and
  `PINARD_POUR_URL`.

- **Usage reporting (TS `turn_end` hook):** the only piece that must live in the
  extension, because per-response usage is only observable in-process. A handler
  accumulates `message.usage` (input/output/cacheRead tokens + `message.model`)
  and PATCHes `contract_stats_url` every **N turns** (default `10`, configurable)
  plus a final flush on `agent_end`. **Tokens + model only** — Mnemosyne prices
  them (pinard's proxy provider registers zero cost rates, so pi's local
  `usage.cost` is always 0 and not used).

- **HTML result posting (babysitter):** on completion of a funded run, the
  babysitter instructs the agent to write a structured report
  (`<rundir>/capsule-report.md`, work summary + issue/MR links), renders it
  md→HTML, prepends a contract header + usage summary, and PATCHes
  `result_patch_url` with `content_type: text/html`. The HTML uses a
  **per-vignoble overridable** template (embedded vineyard-styled default;
  override resolution: vignoble → global → default). This is a **branch in the
  default completion flow gated on funding**, not a new babysitter process.

## Capabilities

### New Capabilities
- `buddy-capsule-redemption`: how a vendangeur is funded by a Buddy Capsule —
  keypair, deterministic redemption + funding resolution, funding gate/poll,
  token-source wiring, usage reporting, and HTML result posting.

### Modified Capabilities
- `aoc`: adds `capsule-keygen`, `capsule-pubkey`, `capsule-redeem` subcommands;
  extends the launch/seed path to resolve a capsule token source.
- `conductor-issue-dispatch`: capsule-gated issues are held (not spawned) until
  funded; lifecycle labels drive the poll set and operator visibility.
- `worker`: adds the usage-reporting hook and the funded-completion report/post
  branch.

## Non-Goals
- No LLM involvement in funding discovery, contract interpretation, or the
  "present pending contract to user" UX.
- No client-side USD cost computation (Mnemosyne prices tokens×model).
- No new capsule creation / funder tooling (Mnemosyne owns that).
- Production Mnemosyne — development targets `https://dev-mnemosyne.example.com`.
