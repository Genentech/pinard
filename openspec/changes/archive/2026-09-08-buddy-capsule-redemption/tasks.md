# Tasks: buddy-capsule-redemption

## 1. Capsule keypair (`aoc`)
- [x] `aoc capsule-keygen`: generate an ed25519 keypair at
      `~/.config/pinard/capsule_key.pem` (mode `0600`); idempotent (no-op if present, `--force` to rotate).
- [x] `aoc capsule-pubkey`: derive and print the raw 32-byte public key as base64
      (DER `pubout` tail-32 → base64), for sharing with contract creators.
- [x] Unit tests for keygen idempotency + pubkey derivation stability.

## 2. Deterministic redemption (`aoc capsule-redeem <contract_id>`)
- [x] Unsigned `GET {MNEM}/actions/{id}/do` funding+authorization probe:
      `patch_url_encrypted != null` (funded) AND `public_key == capsule-pubkey`.
- [x] Signed redemption: build `GET\n/actions/{id}/do\n{ts}\n{nonce}`, ed25519-sign,
      send `Authorization: Signature key=…,timestamp=…,nonce=…,sig=…`; parse `{token, result_key}`.
- [x] AES-GCM-decrypt `patch_url_encrypted` with `result_key` → `result_patch_url`.
- [x] Print bare token to stdout; write `<rundir>/capsule.json`
      `{contract_id, description, result_patch_url, contract_stats_url, result_url}`.
- [x] Stateless/refresh-safe: reuse cached lookup from `capsule.json` on refresh
      (skip probe, re-sign `do` only); fail fast + non-zero on not-funded / key mismatch / non-2xx.
- [x] `PINARD_MNEMOSYNE_URL` (default dev) config; tests for sign/decrypt vectors.

## 3. Funding gate + slow poll (daemon / issue dispatch)
- [x] Detect `contract_id:` in an assigned issue → mark capsule-gated, set
      `capsule:awaiting-funding`, add to poll set; **do NOT spawn**.
- [x] Rebuild poll set on daemon start from issues labeled `capsule:awaiting-funding`.
- [x] Slow poll (`PINARD_CAPSULE_POLL_INTERVAL`, default 20m) → unsigned `/do` check.
- [x] Fast-path: `capsule:funded` label event → immediate re-check.
- [x] On funded → set `PINARD_CAPSULE_CONTRACT`, spawn vendangeur, set `capsule:active`.
- [x] On key mismatch / permanent error → `capsule:failed` with a note.

## 4. Token-source wiring
- [x] `pi-extension/shared/provider.ts` `resolveHelper()`: prefer
      `aoc capsule-redeem $PINARD_CAPSULE_CONTRACT` when the env is set, else pour/apiKeyHelper.
- [x] `cmd/aoc/cmd_launch.go` seed path: seed `auth.json` proxy credential from a
      capsule token when `PINARD_CAPSULE_CONTRACT` is set (mirror `fetchPourToken`).
- [x] Extension typecheck (`cd pi-extension && npm run typecheck`).

## 5. Usage reporting (TS `turn_end` hook)
- [x] `pi.on("turn_end", …)`: accumulate `message.usage` (input/output/cacheRead)
      + `message.model`; count `tool_calls` (tool_execution_end) / `compactions` (session_compact) if cheap.
- [x] PATCH `contract_stats_url` (from `capsule.json`) every `N` turns
      (`PINARD_CAPSULE_STATS_EVERY`, default 10) + flush on `agent_end`; authless JSON-patch.
- [x] No-op when not a capsule run; resilient to PATCH failures (retry/skip, never crash the agent).

## 6. HTML result posting (babysitter)
- [x] Funded-completion branch (gated on `capsule.json` present), not a new process.
- [x] Instruct agent to write `<rundir>/capsule-report.md` (summary + issue/MR/pipeline links).
- [x] Render md→HTML (`goldmark`); wrap in template with contract header + usage summary.
- [x] Overridable template resolution: vignoble → global → embedded default (vineyard-styled),
      via `html/template`; data model `{contract, stats, report, links, vignoble}`.
- [x] PATCH `result_patch_url` with `content_type: text/html`.
- [x] Deterministic fallback report (status + links) if the agent produced none.

## 7. Verify + docs
- [ ] End-to-end against dev-mnemosyne: unfunded issue waits (no spawn, no token spend);
      funding → spawn → run on funder quota → stats visible on contract → HTML result posted.
- [ ] Repeated redemption (refresh) works; revoked/closed contract fails cleanly.
- [x] Update `docs/` (remote-workers / a new capsules page) + skill references.
