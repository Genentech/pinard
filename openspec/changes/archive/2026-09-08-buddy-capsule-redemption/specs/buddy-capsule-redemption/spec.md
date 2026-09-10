## ADDED Requirements

### Requirement: A pinard host has a stable capsule identity keypair
Each pinard host SHALL hold a single ed25519 keypair identifying it as a capsule
redeemer, stored under `~/.config/pinard/` (mode `0600`). `aoc capsule-pubkey`
SHALL print the raw 32-byte public key as base64 for sharing with contract
creators. The private key SHALL never leave the host and SHALL NOT be exposed to
the agent LLM process.

#### Scenario: One-time keygen is idempotent
- **WHEN** `aoc capsule-keygen` runs and no key exists
- **THEN** it creates the keypair under `~/.config/pinard/` with mode `0600`
- **AND** a second run without `--force` leaves the existing key unchanged

#### Scenario: Public key is derivable for sharing
- **WHEN** `aoc capsule-pubkey` runs on a host with a keypair
- **THEN** it prints the base64 raw public key that a funder uses to target this Pi

### Requirement: Capsule redemption is deterministic and LLM-free
Obtaining a capsule token SHALL be performed entirely by `aoc` with no LLM
involvement. `aoc capsule-redeem <contract_id>` SHALL sign
`GET\n/actions/{id}/do\n{timestamp}\n{nonce}` with the host ed25519 key, call the
signed Mnemosyne `do` endpoint, obtain `{token, result_key}`, AES-GCM-decrypt the
result-patch URL, print the bare token to stdout, and persist the non-secret
contract URLs to `<rundir>/capsule.json`.

#### Scenario: Redeem yields a usable token and stashes URLs
- **WHEN** `aoc capsule-redeem <id>` runs for a funded contract whose `public_key`
  matches this host
- **THEN** it prints a bearer token on stdout
- **AND** writes `capsule.json` with `result_patch_url` and `contract_stats_url`

#### Scenario: Redeem is refresh-safe
- **WHEN** `aoc capsule-redeem <id>` is invoked repeatedly (token refresh)
- **THEN** each call re-signs with a fresh timestamp/nonce and returns a valid token
- **AND** cached lookup in `capsule.json` lets refreshes skip the funding probe

#### Scenario: Redeem fails closed on unfunded or wrong agent
- **WHEN** the contract is not funded (`patch_url_encrypted == null`) or its
  `public_key` does not match this host
- **THEN** `capsule-redeem` exits non-zero with a clear message and prints no token

### Requirement: Funded work never spends the operator's own token
A capsule-gated issue (one carrying `contract_id:`) SHALL NOT cause a vendangeur
to be spawned until its contract is confirmed funded via Mnemosyne. No LLM turn
and no operator-token spend SHALL occur while awaiting funding.

#### Scenario: Unfunded contract holds the spawn
- **WHEN** an assigned issue carries a `contract_id` whose contract is not yet funded
- **THEN** the daemon sets `capsule:awaiting-funding` and does not spawn a vendangeur
- **AND** no LLM request is made on the operator's token

#### Scenario: Funding releases the spawn
- **WHEN** the contract becomes funded (`patch_url_encrypted != null`) and the
  `public_key` matches this host
- **THEN** the daemon spawns the vendangeur with `PINARD_CAPSULE_CONTRACT` set
- **AND** transitions the issue to `capsule:active`

### Requirement: The daemon resolves funding via slow poll and label fast-path
The daemon SHALL rebuild its capsule poll set on startup from issues labeled
`capsule:awaiting-funding`, slow-poll Mnemosyne `/do` on a configurable lax
cadence, and additionally re-check immediately when a funder sets a
`capsule:funded` label. Mnemosyne `/do` SHALL be the source of truth; the label
is only a hint.

#### Scenario: Poll set survives daemon restart
- **WHEN** the daemon restarts with pending capsule-gated issues
- **THEN** it reconstructs the poll set from `capsule:awaiting-funding` labels

#### Scenario: Funder label triggers an immediate re-check
- **WHEN** a funder adds the `capsule:funded` label to a waiting issue
- **THEN** the daemon re-checks funding without waiting for the next poll tick
- **AND** a mistakenly-set label with an unfunded `/do` does not spawn

### Requirement: A capsule run reports token usage to its contract
A vendangeur running on a capsule token SHALL report cumulative token usage to the
contract's `contract_stats_url` periodically and at completion. Reported fields
SHALL be token counts and model only; USD cost SHALL NOT be computed client-side.

#### Scenario: Periodic and final usage reporting
- **WHEN** a capsule run completes turns
- **THEN** the agent PATCHes `contract_stats_url` with accumulated
  `input_tokens`/`output_tokens`/`cache_read_tokens`/`model` every N turns
  (default 10) and once more on `agent_end`

#### Scenario: Non-capsule runs report nothing
- **WHEN** a run has no `capsule.json`
- **THEN** no stats PATCH is attempted

### Requirement: A capsule run posts an HTML result to its contract
On completion of a funded run, the babysitter SHALL post a result to the
contract's decrypted `result_patch_url` with `content_type: text/html`. The HTML
SHALL be rendered from an agent-authored markdown report plus a usage summary, via
a per-vignoble overridable template. A deterministic fallback SHALL be posted if
the agent produced no report.

#### Scenario: Report rendered and posted as HTML
- **WHEN** a funded run finishes and the agent wrote `capsule-report.md`
- **THEN** the babysitter renders it to HTML with a contract header + usage summary
- **AND** PATCHes `result_patch_url` with `content_type: text/html`

#### Scenario: Template is overridable per vignoble
- **WHEN** a vignoble provides a `capsule-report.tmpl` override
- **THEN** it is used in preference to the global override and the embedded default

#### Scenario: Fallback result when no report was produced
- **WHEN** a funded run ends without an agent-authored report
- **THEN** the babysitter posts a minimal deterministic result (status + issue/MR links)
