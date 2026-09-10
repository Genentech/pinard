# Design: mr-knowledge-ingestion

## Context

Pinard memory has three write paths today: `/lesson` (manual `rule`/`fact`
entities, `provenance=lesson`), `/teaching` + passive Engram summaries (LLM
extraction → entities), and the wiki/Ledger (git-backed markdown, chunked into
`wiki_chunk`). Merged MRs — the richest record of *why a change was made* — are
not captured. This design routes them into the existing extraction pipeline as a
new provenance-tagged source, deliberately **extracting decisions rather than
storing diffs**.

## Goals / non-goals

**Goals**
- Capture the decision + rationale of *merged* MRs — read together with their
  issue(s) — as atomic, recall-ready entities.
- Reuse the existing ingester extraction, ontology, and supersession machinery,
  with an **MR-specific prompt**.
- Keep noise out (mechanical MRs, transient review chatter) and avoid
  contradictory accumulation (supersession).
- Idempotent, scoped per repo, fail-open, conservative (may emit nothing).

**Non-goals**
- No ingestion of raw diff hunks (code ≠ knowledge).
- No chunking of MR content (entities stay atomic / single-embedding).
- No ingestion of un-merged/closed MRs (merged = validated signal).
- **No auto-extraction from review discussion in v1** (mostly transient process;
  see §Discussion for the human-gated future path).
- No new capture UI — this is an automatic pipeline.
- No LLM or GitLab calls from the host; no reach-back to GitLab from the cloud
  ingester (see §Where it runs).

## Trigger and event shape

The `mr-watcher` (host) already tracks MRs and detects the merged transition (it
emits `mr_merged` today). On that transition, for MRs that pass the **noise
filter**, the host fetches the MR and its **closing issue(s)** via the GitLab
client and publishes a memory event on the existing memory subject
(`pinard.<vignoble>.memory.mr`, alongside `…memory.rules`) with a compact,
knowledge-oriented payload:

```jsonc
{
  "source": "mr",
  "project": "<repo>",          // e.g. your-org/pinard/pinard
  "iid": 355,
  "scope": "<vigne/repo scope>",
  "title": "fix(memory): recall returns no hits for irrelevant queries; …",
  "description": "<MR description markdown>",
  "issues": [                    // ALL issues the MR closes (Closes #N), as CONTEXT
    { "iid": 193, "title": "…", "description": "<issue description markdown>" }
  ],
  "files_changed": ["pi-extension/pinard/index.ts", "services/memory/recall_service.py", "…"],
  "merged_at": "2026-08-28T…Z",
  "author": "pinard-bot"
}
```

Explicitly **absent**: diff hunks and review discussion. `files_changed` is the
file-path list only (an optional one-line-per-file summary MAY be added later; the
diff body is never shipped). The issue **description** (not just its title) is
included as context so the extraction can read intent-vs-realization; if the MR
closes no issue, the `issues` array is empty and extraction proceeds on the MR
alone.

## What gets extracted

The ingester `mr` handler feeds **MR title + description** (the realized change)
with the **closing issue(s) title + description as context** (the intent) to the
LLM extraction. It does **not** use the generic ontology-extraction prompt (that
prompt has no notion of durable-vs-transient and would, e.g., turn "you forgot to
implement X" into a `task` entity). Instead it uses an **MR-specific prompt**:

> You are given the original goal (issue) and how it was realized (the merged MR).
> Extract only **durable** knowledge as atomic entities:
> - `decision` — a choice made and *why* ("chose X over Y because Z");
> - `artifact` — a durable structural fact the change established ("recall returns
>   empty when nothing passes the distance gate");
> - `diagnosis` — a root cause paired with its fix (issue: cause → MR: fix).
> **Capture re-scope deltas**: if the change diverged from the issue's stated
> intent, record it ("the issue assumed X; the change did Y because Z").
> **Ignore** transient/process content: TODOs, "you forgot X", renames, rebases,
> style nits, CI, approvals, and restatements with no rationale.
> If there is no durable decision — **or you are in any doubt** — **return no
> entities.** A zero-entity result is expected and correct for trivial changes;
> prefer missing a marginal item over capturing noise.

The issue is **context only** — entities are grounded in the merged MR (validated
ground truth); the extraction does not emit "what we wanted" as fact.

All emitted entities carry `provenance = "mr"` and a `data` blob referencing the
source (`{mr: "<project>!<iid>", issues: […], merged_at, url}`) for traceability
and `/recall fetch`. `files_changed` is stored in `data` (not embedded) so recall
can show "what it touched" without polluting the vector.

Entities remain **atomic, single-embedding, unchunked** — a long MR/issue is
*reduced* to its decisions, never chunked as a document. (If a change's knowledge
is genuinely document-shaped, it belongs in the wiki/Ledger, which already has the
chunked pipeline.)

**Zero-entity outputs are normal.** Many MRs (small fixes, config tweaks) carry no
durable decision; the handler treats an empty extraction as success and writes
nothing — consistent with the recall "empty is fine" philosophy.

## Where it runs (host vs cloud)

The pipeline splits across the same boundary as every other capture path
(`/lesson`, teaching episodes):

- **Host — per-vignoble `aoc daemon` / `mr-watcher` (`internal/watcher/mrs.go`).**
  Detects the merge, **fetches** the MR + its closing issue(s) via the GitLab
  client (the host already holds the tokens), applies the **noise filter**, and
  **publishes** the assembled text to `pinard.<vignoble>.memory.mr`. No LLM, no
  SurrealDB, no diff hunks.
- **Cloud — central memory service (the `pinard-uat-memory` deployment).** The
  ingester **consumes** the event and runs the **LLM extraction** (its configured
  `MEMORY_LLM_*` model) + embedding, writing entities/edges to the cluster
  SurrealDB. The ingester does **not** reach back to GitLab — all it needs is in
  the event payload (hence issue+MR text is included by the host).

Consequences: extraction cost/latency stay **central** (not on the conductor's or
a worker's token); the merge **never blocks** on the LLM (async over NATS); and no
new credential surface is introduced.

## Noise filter

Skip mechanical MRs before publishing the event. v1 heuristics (configurable):

- **Title regex skip:** `^(feat|fix)\(docs\): sync Ledger`, `bump-image`,
  `bump-chart`, `^Revert `, cuvee accumulation merges (`cuvee/…` source branch).
- **Version-only / generated:** MRs whose description is empty or matches the
  bump/release template.
- **Size guard:** optionally skip trivial MRs (e.g. only lockfile/version files
  changed).
- **Label opt-out/opt-in:** honour an explicit `memory:skip` label (never ingest)
  and `memory:capture` label (always ingest, bypassing heuristics).

The filter lives in `mrs.go` (so no event is even published for noise) and is kept
deliberately small; false-negatives (a noise MR slips through) are harmless-ish,
false-positives (dropping a real decision) are the thing to avoid — so heuristics
are conservative and label-overridable.

## Supersession

Contradictory accumulation is the main long-term risk (e.g. the `cli-*`
add-then-revert). The ingester already models `supersedes` edges. On extracting an
MR decision, when it maps to the same `(role, name)` (subject) as an existing
entity, or the LLM identifies it as replacing a prior decision, create a
`supersedes` edge (new → old) and let recall prefer the newest. Reverts are a
special case: a `^Revert` MR (if it passes the filter via `memory:capture`) SHOULD
supersede the decision it reverts. Recall surfaces the current decision and can
show "(supersedes: …)".

## Two-pass extraction; review is a Phase-2 delta pass

Extraction is structured as two passes so the noisy channel (review) never dilutes
the clean one (description), and so "return nothing" is the natural default:

- **Pass 1 — issue + MR description (v1).** The high-signal, curated prose. Emits
  the baseline decisions with the prompt above ("if in doubt, return nothing").

- **Pass 2 — review discussion (Phase 2, not v1).** Run *only after* pass 1 and
  **given pass 1's output as context**, as a **delta** pass with a deliberately
  narrow question:
  > Here are the decisions already captured from this change. Reading the review,
  > is there any **new, durable** decision or constraint **not already covered**?
  > If not — **or if you are in any doubt** — **return nothing.**

  The delta framing kills redundancy (most reviews only re-confirm the
  description) and makes zero-output the default, countering a cheap model's
  eagerness. Guardrails:
  1. **Heuristic pre-filter** the notes before the LLM: drop bot notes, the
     `pinard:*` markers (webterm-link/conductor), and pure "LGTM / tests pass /
     pushed / rebase / CI" lines.
  2. **Require a "why-durable" justification** per emitted item (same call); drop
     unjustified ones.
  3. **Lower confidence / `provenance = "mr-review"`** so recall can weight them
     and a human can prune (they are `manual_edit`-editable per #194).

  Cheap models (the memory service's `MEMORY_LLM_*` — Gemini Flash / Haiku) are
  adequate here *because* of the delta framing + pre-filter + precision bias; two
  passes per (noise-filtered) MR is affordable.

  **Worked example — MR !364** (`aoc attach`): pass 1 (issue #65 + description)
  yields "read-only PTY viewer; NATS-creds trust boundary; read-only via
  `attach -r`". Pass 2 over the security-pass thread surfaces the genuinely
  net-new, load-bearing constraint the description lacked — *"`pty.out` is a
  stable, non-grant-scoped subject; in multi-tenant (#28/#29) it MUST be scoped in
  per-tenant NATS account permissions"* — while correctly ignoring "tests pass /
  pushed a commit / merge once green". Notably the reviewer *also* flagged it by
  hand ("load-bearing … so it's not lost"), which is exactly the marker path below.

### Always-on: human-gated `@memory:` marker
Orthogonal to the passes, a reviewer can mark a specific note (`@memory: <point>`
or a note-level `memory:capture`) and *that text* routes to the existing
**`/lesson`** pipeline — high precision, zero classification guesswork. This is the
gold path for load-bearing points and can ship independently of Pass 2.

## Idempotency, scope, ordering

- **Idempotent** per `<project>!<iid>`: re-delivery/replay of the same merged MR
  is a no-op (upsert by deterministic entity key + a processed-MR marker /
  cursor). Aligns with the ingester's existing cursor model.
- **Scope** = the repo's vigne scope; important because multi-repo vignobles
  (`misc` → 5 repos) must not cross-contaminate. Cross-repo MR-IID collisions are
  avoided by keying on `<project>!<iid>`.
- **Fail-open:** any failure (GitLab fetch, LLM unavailable, NATS) logs and drops
  the single MR event; it never blocks merges or the watcher loop. LLM-unavailable
  handling mirrors teaching-episode extraction (defer/skip, not crash).

## Interaction with #194 (editable entities)

MR-sourced entities are extraction-sourced, so they are subject to the same
re-extraction/`--reingest` overwrite as any extracted entity — and thus protected
by #194's `manual_edit` guard once a human edits them. Re-ingesting the same MR is
idempotent; a human-curated MR-derived decision is not clobbered.

## Alternatives considered

- **Store the MR description verbatim as one entity.** Rejected: descriptions are
  document-shaped and would either bloat a single embedding (diluting recall) or
  tempt chunking (violating the atomic-entity boundary). Extraction yields sharper,
  atomic, recall-ready facts.
- **Ingest diffs.** Rejected: code is noise for semantic recall; the knowledge is
  in the rationale, not the hunks.
- **Ingest on open, not merge.** Rejected: only merged MRs are validated;
  ingesting proposals would capture rejected/abandoned approaches as if adopted.
- **Make it a wiki pipeline instead of entities.** Rejected for the decision
  content (atomic → entity); the Ledger already covers the document-shaped
  "shipped features" view.

## Open questions

1. **Files-changed summary** — file list only (v1) vs. one-line-per-file LLM
   summary (later)? Start with the list in `data`.
2. **Issue fetch shape** — use GitLab's closing-issues list for the MR, or parse
   `Closes #N` from the description? Prefer the API's closing-issues list; fall
   back to parsing. Cross-project `Closes` links are rare — v1 may ignore them.
3. **Cross-repo dedup for the same logical change** (e.g. a decision spanning two
   repos) — likely out of scope for v1.
4. **Retro-ingestion** — a one-off backfill of historical merged MRs (bounded,
   filtered) vs. forward-only from rollout? Recommend forward-only first.
5. **Discussion capture ergonomics** — exact marker for the deferred human-gated
   path (`@memory:` prefix vs. a note label) and how the host detects it. Deferred
   with the discussion phase.
