# Design: buddy-capsule-redemption

## Context

Mnemosyne endpoints (dev: `https://dev-mnemosyne.example.com`):

- `GET /actions/{contract_id}/do` — **dual behavior**:
  - **Unsigned** → contract view: `{description, public_key, result_url,
    patch_url_encrypted}`. `patch_url_encrypted == null` ⇔ **not funded**.
  - **Signed** (`Authorization: Signature key=<pubB64>,timestamp=<ts>,nonce=<uuid>,sig=<b64>`)
    → redemption: `{token, result_key, ...}`.
  - Signature message: `GET\n/actions/{action_id}/do\n{timestamp}\n{nonce}`
    (ed25519 over UTF-8 bytes; `action_id` = the `{contract_id}` path segment).
  - Repeated redemption is allowed (same as today's dev `/do` URLs) — this is the
    refresh mechanism.
  - **Signed `/do` status semantics:**
    | Status | Meaning | Action |
    |--------|---------|--------|
    | 2xx | Success — body contains `{token, result_key}` | Proceed |
    | 401 | Agent token expired — re-mint signature and retry once | Refreshable |
    | 404 / 410 | Budget exhausted or contract gone | `ErrCapsuleExhausted` |
    | 4xx (other) | Permanent client error | Fatal |
    | 5xx | Transient server error | Retry next cycle |
- `PATCH {contract_stats_url}` — authless JSON-patch: `[{"op":"replace",
  "path":"/stats","value":{input_tokens,output_tokens,cache_read_tokens,
  tool_calls,compactions,model}}]`.
- `PATCH {result_patch_url}` — capability URL (decrypted from
  `patch_url_encrypted` with `result_key`): `[{"op":"replace","path":"/content_type",
  "value":"text/html"},{"op":"replace","path":"/data","value":"<html>"}]`.

Reference: `docs/skills/capsule-agent.md` and `docs/capsules.md` in
`gitlab.example.com/artifactdb/mnemosyne`.

## Key decisions

### D1 — All crypto in Go, none in TS
`crypto/ed25519` (sign) and `crypto/cipher` AES-GCM (decrypt) are stdlib.
`aoc capsule-redeem` does lookup + sign + decrypt and prints a bare token, exactly
like `fetchPourToken`. The extension never handles keys or ciphertext. Rationale:
determinism, single audited code path, no secrets in the LLM process.

### D2 — Redemption is the token source (the "pour equivalent")
Wire `aoc capsule-redeem <id>` into `resolveHelper()` (`pi-extension/shared/provider.ts`)
as a third source. `callHelper()` already runs an arbitrary command, parses JWT
`exp`, and re-invokes on refresh — so a capsule token refreshes by re-redeeming.
`capsule-redeem` must therefore be **stateless + lean**: cache the lookup
(`result_url`, `contract_stats_url`, `public_key`) in `capsule.json` on first
redeem so refreshes only re-sign the `do` call (1 round-trip).

Selection: a `PINARD_CAPSULE_CONTRACT` env (set by the daemon at spawn) makes
`resolveHelper()` prefer `aoc capsule-redeem $PINARD_CAPSULE_CONTRACT` over
`PINARD_POUR_URL`. If unset → existing pour/apiKeyHelper behavior.

### D3 — Never spend the operator token on funded work → gate the spawn
Because a funded run must not use our token, the **gate is before spawn**, not
inside the agent. The daemon:
1. Detects `contract_id:` in an assigned issue → marks it capsule-gated, sets
   `capsule:awaiting-funding`, adds it to the poll set. **Does not spawn.**
2. Re-checks funding when (a) the slow poll ticks, or (b) a `capsule:funded`
   label event arrives (fast path). Truth = unsigned `/do`
   (`patch_url_encrypted != null` AND `public_key == aoc capsule-pubkey`).
3. On funded → sets `PINARD_CAPSULE_CONTRACT`, spawns the vendangeur, sets
   `capsule:active`.

### D4 — Labels as durable poll-set state
No new persistence: on daemon start, list issues labeled
`capsule:awaiting-funding` (assigned to pinard) to rebuild the poll set.
Lifecycle: `awaiting-funding` → `active` → `spent` | `failed`. Poll cadence is
lax and configurable (default 20 min); a small per-issue backoff/next-poll
timestamp may live in NATS KV but is not required for correctness.

### D5 — Usage: tokens + model only, from `turn_end`
`pi.on("turn_end", e => e.message.usage)` gives `{input, output, cacheRead,
cacheWrite, reasoning?, totalTokens}` + `e.message.model`. Accumulate and PATCH
`contract_stats_url` every `N` turns (default 10, `PINARD_CAPSULE_STATS_EVERY`)
and flush on `agent_end`. **Cost is intentionally omitted**: pi computes
`usage.cost` locally via `calculateCost(model, usage)` from `model.cost` rates,
and pinard's proxy provider registers zero rates, so it is always 0. Mnemosyne
prices tokens×model. `tool_calls`/`compactions` can be derived from
`tool_execution_end` / `session_compact` events if cheap; otherwise sent as 0.

### D6 — Result: agent writes markdown, babysitter renders HTML
The funded-completion branch:
1. Instructs the agent (one final turn) to write `<rundir>/capsule-report.md`
   (work summary + issue/MR/pipeline links).
2. Renders md→HTML (Go `goldmark`), wraps in a template with a contract header +
   usage summary table (from `capsule.json` + last stats).
3. PATCHes `result_patch_url` with `content_type: text/html`.
Template resolution (overridable, not hard-coded):
`~/.config/pinard/vignobles/<v>/capsule-report.tmpl` → `~/.config/pinard/capsule-report.tmpl`
→ embedded vineyard-styled default. Data model: `{contract, stats, report(html),
links, vignoble}` via Go `html/template`.

## Flow (happy path)

```
funder: create contract for issue #N (agent pubkey) ── fund ── [set capsule:funded]
  │
pinard daemon: sees contract_id in #N → capsule:awaiting-funding, poll set (NO spawn)
  │  (slow poll  OR  capsule:funded label event)
  ▼
unsigned GET /do → funded? key match?  ──no──► keep waiting
  │yes
  ▼
spawn vendangeur with PINARD_CAPSULE_CONTRACT=<id> → capsule:active
  │
apiKeyHelper = aoc capsule-redeem <id> → signed /do → token (refresh re-redeems)
  │  provider proxy runs on funder's quota
  ├─ turn_end hook: accumulate usage → PATCH contract_stats_url every N + on agent_end
  ▼
completion: agent writes capsule-report.md → babysitter md→HTML + stats → PATCH result_url (text/html)
  │
capsule:spent
```

## Risks / open points
- **Report trigger reliability**: the final report turn must run even on
  error/abort paths where possible; if the agent can't produce a report, post a
  minimal deterministic fallback (status + links) so the funder always gets a
  result. Fits the fail-loud posture from the babysitter work.
- **Refresh storms**: ensure `capsule-redeem` is only invoked near JWT expiry
  (pi already gates on `exp`); keep it a single signed round-trip on refresh.
- **Key/authorization mismatch**: if `/do.public_key` ≠ local pubkey, do not
  spawn; set `capsule:failed` with a clear note (wrong agent targeted).
- **dev vs prod base URL**: `PINARD_MNEMOSYNE_URL` (default dev) via env /
  credentials.yaml; no hard-coded host.
