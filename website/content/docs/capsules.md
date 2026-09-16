---
title: Capsule Protocol
weight: 41
group: Applications
---

The **Capsule Protocol** lets one person fund an agent's LLM quota for a specific task
without sharing provider credentials. A **contract owner** creates a *ContractAction*
for an agent's public key. A **funder** then attaches a finite *CapsuleAction* budget to
that contract through any compatible funding client. These are distinct protocol roles,
although the same person may perform both.

The protocol is application-agnostic. Any client can create or fund a contract, and any
agent harness that can keep an ed25519 private key and make HTTP calls can redeem it.
Pinard can create contracts as well as execute them; [Pinard integration](#pinard-integration)
explains its pre-spawn gate, vendangeur runtime, reporting, and parking behavior.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/capsule-protocol-sequence-v2.png" alt="Six protocol actors above a six-step sequence: contract owner, funder, Mnemosyne, Pinard daemon, vendangeur, and LLM provider.">
    <span class="doc-figure-label charcoal" style="--x: 8.3%; --y: 28%;">Contract owner</span>
    <span class="doc-figure-label terracotta" style="--x: 25%; --y: 28%;">Funder</span>
    <span class="doc-figure-label mustard" style="--x: 41.7%; --y: 28%;">Mnemosyne</span>
    <span class="doc-figure-label mustard" style="--x: 58.3%; --y: 28%;">Pinard daemon</span>
    <span class="doc-figure-label mustard" style="--x: 75%; --y: 28%;">Vendangeur</span>
    <span class="doc-figure-label charcoal" style="--x: 91.7%; --y: 28%;">LLM provider</span>
    <div class="capsule-sequence" aria-label="Capsule protocol and Pinard integration sequence">
      <div class="capsule-sequence-step"><span class="capsule-sequence-number">1</span><strong>Owner → Mnemosyne</strong><span>Create a contract for the agent key and publish its contract ID.</span></div>
      <div class="capsule-sequence-step terracotta"><span class="capsule-sequence-number">2</span><strong>Funder → Mnemosyne</strong><span>Attach a finite Capsule budget through a compatible funding client.</span></div>
      <div class="capsule-sequence-step"><span class="capsule-sequence-number">3</span><strong>Agent ↔ Mnemosyne</strong><span>Read the contract first; when funded, discover the active Capsule.</span></div>
      <div class="capsule-sequence-step"><span class="capsule-sequence-number">4</span><strong>Agent ↔ Mnemosyne → provider</strong><span>Sign redemption, obtain a short-lived token, and perform model work.</span></div>
      <div class="capsule-sequence-step terracotta"><span class="capsule-sequence-number">5</span><strong>Agent → Mnemosyne</strong><span>Report cumulative usage periodically and post the final result.</span></div>
      <div class="capsule-sequence-step"><span class="capsule-sequence-number">6</span><strong>Pinard integration</strong><span>Gate before spawn; run in a vendangeur; park safely on exhaustion.</span></div>
    </div>
  </div>
  <figcaption><strong>The first five steps are the portable protocol; the sixth is Pinard's runtime integration.</strong> Contract creation and funding remain separate even when one person performs both roles.</figcaption>
  <ol class="doc-figure-legend doc-figure-legend--column-major" aria-label="Capsule protocol sequence">
    <li><span class="doc-figure-key">1</span><span><strong>Create contract</strong> — any compatible client, including Pinard, can register the task and agent public key.</span></li>
    <li><span class="doc-figure-key">2</span><span><strong>Fund contract</strong> — a funder uses a compatible client to create the live CapsuleAction budget.</span></li>
    <li><span class="doc-figure-key">3</span><span><strong>Verify & discover</strong> — read the ContractAction, confirm the key and funding marker, then look up the Capsule.</span></li>
    <li><span class="doc-figure-key">4</span><span><strong>Redeem & work</strong> — the agent signs with its private key and spends only the returned provider token.</span></li>
    <li><span class="doc-figure-key">5</span><span><strong>Report & finish</strong> — usage and result content return to Mnemosyne without blocking the work loop.</span></li>
    <li><span class="doc-figure-key">6</span><span><strong>Pinard-specific</strong> — the daemon gates spawning; the vendangeur redeems; exhausted work is parked without fallback credentials.</span></li>
  </ol>
</figure>

---

## 1. Roles and data model

| Concept | What it is |
|---------|-----------|
| **Contract owner** | Creates the task declaration and publishes its `contract_id`. May use Pinard, another client, or the Mnemosyne API. |
| **ContractAction** | A stable task declaration in Mnemosyne. It contains the job description and agent public key. Its Mnemosyne `action_id` is the `contract_id`. |
| **Funder** | Attaches LLM quota to an existing contract through any compatible funding client. Pays from their own quota but need not be the contract owner. |
| **CapsuleAction** | A live, finite quota grant associated with the ContractAction. It has its own action and signed `do_url`; clients discover it by the contract's `contract_id`. |
| **Redeeming agent** | The agent harness instance that holds the ed25519 private key matching the contract's `public_key`. |
| **Agent identity keypair** | A raw ed25519 keypair generated once per agent. The 32-byte public key is base64-encoded in the ContractAction; the private key never leaves the agent host. |

The ContractAction and CapsuleAction deliberately have different lifetimes. Read the
stable contract first to verify the task and agent key; only after it reports funding
should the agent discover the current live Capsule.

---

## 2. Create, fund, verify, and discover

### 2a. Create the contract

The contract owner creates a ContractAction containing the work description and the
agent's public key. The returned Mnemosyne action ID is the `contract_id` published with
the task. Contract creation is client-independent; Pinard's creation path is documented
under [Pinard integration](#creating-a-contract-with-pinard).

### 2b. Fund the contract

The funder supplies that `contract_id` to any compatible funding client, which creates
a CapsuleAction with a finite call budget. Funding does not transfer provider credentials
to the contract owner or agent.

### 2c. Read and verify the contract

Before capsule lookup, the agent reads:

```
GET {MNEM}/actions/<contract_id>/do
```

No authentication is required. The response includes `description`, `public_key`,
`result_url`, and `patch_url_encrypted`.

- `patch_url_encrypted: null` means the contract is not funded yet.
- A base64 string means funding exists and the agent may continue to lookup.
- `public_key` MUST match the agent's own key; a mismatch is a permanent failure.
- Network and 5xx errors are transient and should be retried.

### 2d. Discover the live Capsule

```
GET {MNEM}/capsules/lookup?contract_id=<id>
```

No authentication required. Response (200 OK):

```json
{
  "do_url":             "<url to the capsule's signed /do endpoint>",
  "calls_allowed":      10,
  "calls_succeeded":    3,
  "claimed":            true,
  "result_url":         "<funder-visible result page URL>",
  "patch_url_encrypted": "<base64 AES-GCM ciphertext of result_patch_url>",
  "contract_stats_url": "<URL for usage reporting>"
}
```

Status codes:

| HTTP | Meaning |
|------|---------|
| 200  | An active Capsule exists; use its `do_url` and metadata |
| 404  | No active Capsule: not funded yet, or a previously active Capsule is exhausted |
| 409  | Multiple active Capsules; stop and contact the funder |
| 5xx  | Transient server error — retry |

The `do_url` belongs to the CapsuleAction, not the ContractAction. `result_url` is public;
`patch_url_encrypted` protects the write URL; and `contract_stats_url` receives usage.

---

## 3. Agent-side signed redemption

Once the agent has verified the contract (§2c) and discovered the Capsule (§2d), it redeems the
capsule by performing a **signed GET** to the capsule's `do_url` (obtained from the
lookup response).

### Signing

Construct the message to sign (newline-separated, no trailing newline):

```
GET\n<path>\n<ISO-8601 timestamp>\n<nonce>
```

Where:
- `<path>` is the URL path component of `do_url` (e.g. `/actions/<id>/do`) — **not** the
  full URL with query string.
- `<ISO-8601 timestamp>` is the current UTC time in `2006-01-02T15:04:05Z` format.
- `<nonce>` is any unique string for this request; UUID v4 is recommended. Mnemosyne
  rejects duplicates seen within 120 seconds.

Sign the UTF-8 encoding of this message with the agent's ed25519 private key. Encode the
signature as standard base64.

### Request

```
GET <do_url>
Authorization: Signature key=<pubkeyB64>,timestamp=<ts>,nonce=<nonce>,sig=<sigB64>
```

- `<pubkeyB64>` — base64-encoded raw 32-byte ed25519 public key (matching the contract).
- `<ts>` — the same ISO-8601 timestamp used in the signed message.
- `<nonce>` — the same nonce used in the signed message.
- `<sigB64>` — base64-encoded ed25519 signature.

### Atomic first claim

On the first successful redemption, Mnemosyne atomically records the presented public
key as the Capsule's `claiming_key`. If two agents race, exactly one succeeds. Later
redemptions must be signed by the same key.

### Portable response

The portable protocol guarantees a successful response containing the short-lived
provider token. The current Pinard/Mnemosyne integration also accepts a JSON envelope on
first claim:

```json
{
  "token":      "<short-lived JWT for the LLM provider>",
  "result_key": "<base64 AES-256 key>",
  "owner":      "<funder username>"
}
```

- `token` — the LLM token to use for this session. It is short-lived; the agent MUST
  re-redeem (§3) on token expiry to obtain a fresh one.
- `result_key` — present only on first claim. Used to decrypt `patch_url_encrypted`
  (§4). On refresh responses this field is absent.
- `owner` — the funder's identity (Mnemosyne v0.21.0+); may be absent.

### Response (refresh)

On token refresh, the current integration may return a bare JWT string instead of the
JSON envelope:

```
eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl
```

Pinard accepts both forms:

1. Try JSON parse first; if `token` is non-empty, use it.
2. Otherwise, if the response starts with `eyJ` and contains exactly two `.` separators,
   treat the whole body as a bare JWT.

On refresh, `result_key` is absent. The client MUST reuse the `result_patch_url` cached
from the first-claim response (§4).

### Redemption status codes

| HTTP | Protocol meaning |
|------|------------------|
| 403 | Missing or invalid signature, stale timestamp, replayed nonce, or wrong claiming key |
| 410 | Capsule expired or was revoked |
| 429 | Fuel exhausted (`calls_allowed` reached) |

The timestamp may differ from server time by at most 60 seconds. The nonce deduplication
window is 120 seconds.

---

## 4. Result key and result posting

`patch_url_encrypted` (from the lookup response, §2d) is an **AES-GCM ciphertext** of
the URL to which the agent should post its result. The nonce is prepended to the
ciphertext (standard nonce-prepend convention).

Decrypt on first claim:

1. Base64-decode `patch_url_encrypted` → raw ciphertext bytes.
2. Base64-decode `result_key` → 32-byte AES-256 key.
3. AES-GCM decrypt: split the first `NonceSize()` (12) bytes as the nonce; decrypt the
   remainder with no additional data.
4. The plaintext is the `result_patch_url` (a URL string; trim whitespace).

Cache `result_patch_url` locally (e.g. in a run-state file). On refresh, `result_key` is
absent — the cached URL is reused.

### Posting the result

When the task is complete, PATCH its result to
`result_patch_url`:

```
PATCH <result_patch_url>
Content-Type: application/json

[
  {"op": "replace", "path": "/content_type", "value": "text/markdown"},
  {"op": "replace", "path": "/data",         "value": "<result content>"}
]
```

No authentication is required for this PATCH because the unguessable URL is itself the
credential. The result may be Markdown, HTML, JSON, or another agreed MIME type.

---

## 5. Usage reporting

While work is in progress, the agent SHOULD periodically report token usage to allow
the funder to monitor consumption. This is authless:

```
PATCH <contract_stats_url>
Content-Type: application/json

{
  "model":             "claude-sonnet-4-5",
  "input_tokens":      12345,
  "output_tokens":     6789,
  "cache_read_tokens": 1000,
  "tool_calls":        42,
  "compactions":       1
}
```

Reporting is advisory. PATCH failures MUST be logged but MUST NOT crash or stop the
agent. A final flush SHOULD be sent when the agent session ends.

---

## 6. Lifecycle and failure semantics

```
contract created → unfunded → funded → active → completed
                                               ↘ exhausted → [refunded?] → active
                    ↘ mismatch/error → failed (permanent)
```

The following are **protocol requirements** (MUST / MUST NOT):

| Situation | Required behaviour |
|-----------|-------------------|
| No capsule (404 on lookup) — not yet funded | Agent MUST NOT run. Wait for funding. |
| Funded and pubkey matches local key | Agent MAY run, using only the capsule token. |
| Pubkey mismatch — contract targets a different agent | Agent MUST fail permanently. MUST NOT run on any other token. |
| Permanent contract or authorization error | Agent MUST fail permanently. MUST NOT run. |
| Capsule exhausted (`429` on signed `/do`) | Agent MUST stop immediately. MUST NOT fall back to an operator or default token. |
| Capsule expired or revoked (`410` on signed `/do`) | Agent MUST stop immediately. |
| Capsule exhausted (404 on lookup, after previously being funded) | Same as above — treat as exhaustion, not "not yet funded". |
| 5xx / transient errors on either probe endpoint | Retry; do not treat as permanent. |

**Fail-closed invariant**: when an agent is capsule-gated (a contract is associated with
the work item), it MUST NEVER acquire a token from any other source. The funder's quota
is the only permitted source. An operator token, a default API key, or any other fallback
MUST NOT be used, regardless of capsule state.

**Parking vs. failure**: a temporarily exhausted capsule MAY be refilled by the funder.
An implementation SHOULD "park" the run (persist state so it can be resumed) rather than
closing it permanently. A pubkey mismatch or permanent authorization error, however, is a
permanent failure.

---

## Pinard integration

Pinard can create the ContractAction and can run the agent that redeems the resulting
Capsule. The mechanics below are Pinard-specific and are not portable protocol
requirements.

### Where Pinard participates

1. The régisseur or `aoc capsule-contract` creates a ContractAction and publishes its ID
   on the GitLab issue.
2. An external, compatible funding client creates the CapsuleAction for that ID.
3. The daemon waits for a funded, matching contract before it spawns a vendangeur.
4. The vendangeur calls `aoc capsule-redeem`, reports usage, and posts the result.
5. Pinard parks exhausted work and never substitutes an operator token.

**Compatibility note:** the current daemon checks Capsule lookup before it probes the
ContractAction, while the portable protocol specifies contract-first ordering. Both
checks must succeed before Pinard spawns work. Current Pinard also accepts the deployed
Mnemosyne compatibility responses `401`, signed-`/do` `404`, and `410`; these should not
be treated as portable status-code definitions.

### Build tag requirement

The capsule commands (`aoc capsule-keygen`, `aoc capsule-pubkey`, `aoc capsule-contract`,
`aoc capsule-redeem`) and the capsule-aware daemon logic are compiled only when `aoc` is
built with `-tags capsule`. The release bundle handles this automatically. When building
from source, `./install` detects the capsule source files and adds the tag automatically.

### Identity keypair

```bash
aoc capsule-keygen          # generates ~/.config/pinard/capsule_key.pem (mode 0600)
aoc capsule-pubkey          # prints the base64 public key to share with funders
aoc capsule-keygen --force  # rotate (existing contracts tied to old key will be declined)
```

### Creating a contract with Pinard

The régisseur's `create_contract` tool authenticates to Mnemosyne via device-auth flow
(tokens cached at `~/.config/pinard/mnemosyne-tokens.json`), creates the ContractAction
with the host's pubkey, and posts a structured comment on the GitLab issue containing the
`contract_id:` line that the daemon detects automatically.

From the CLI:

```bash
aoc capsule-contract \
  --title "Add GWAS pipeline stage" \
  --description "Implement imputation for issue #42" \
  --repo mygroup/myproject \
  --issue 42
```

### Lifecycle labels (durable poll state)

| Label | Meaning |
|-------|---------|
| `capsule:awaiting-funding` | Contract detected; daemon waiting for funding before spawning |
| `capsule:funded` | Funder-set fast-path hint; daemon re-checks immediately |
| `capsule:active` | Vendangeur spawned, work in progress |
| `capsule:spent` | Run completed, result posted |
| `capsule:failed` | Permanent failure (pubkey mismatch, authorization error) |
| `capsule:exhausted` | Budget exhausted; issue re-labeled `awaiting-funding` for refund |

The daemon rebuilds its poll set from GitLab labels on restart — labels are the durable
state, not in-memory structures.

### Run state file

`aoc capsule-redeem <contract_id>` (called by the vendangeur at token-refresh time)
writes `<rundir>/capsule.json`:

```json
{
  "contract_id":       "<id>",
  "do_url":            "<cached do_url for refresh>",
  "result_patch_url":  "<decrypted result posting URL>",
  "contract_stats_url":"<URL for usage PATCH>",
  "result_url":        "<funder-visible result page>"
}
```

On token refresh the daemon skips the lookup round-trip and re-signs `do_url` directly.

### Environment variables

| Variable | Set by | Purpose |
|----------|--------|---------|
| `PINARD_CAPSULE_CONTRACT` | Injected at spawn | Contract ID for `capsule-redeem` |
| `PINARD_MNEMOSYNE_URL` | Operator (**required**) | Mnemosyne base URL — there is no default. Set in `~/.config/pinard/env` on the host running `aoc daemon`. |
| `PINARD_CAPSULE_STATS_EVERY` | Operator | Usage reporting interval in turns (default: 10) |

### Capsule-aware orphan recovery

When the daemon's orphan recovery restarts a dead capsule run, it probes
`/capsules/lookup` first. If the capsule is exhausted (404/410), it parks the run and
relabels the issue `capsule:awaiting-funding` instead of respawning. It will never
restart a capsule run on the operator token.

---

## See also

- **[The SWE Process](/docs/swe-process/)** — the overall issue → MR → merge loop.
- **[Remote Workers](/docs/remote-workers/)** — sandboxed workers (HPC/Singularity) that also support capsules.
- **[Web Terminal](/docs/web-terminal/)** — browser access to live vendangeur sessions.
