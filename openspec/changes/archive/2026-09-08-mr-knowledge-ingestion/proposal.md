# Change: mr-knowledge-ingestion

## Why

A **merged merge request** is one of the highest-signal knowledge sources a
Pinard fleet produces, and today it is thrown away. An MR bundles the three
things memory most wants to preserve:

- the **decision** — its title (`fix:`/`feat:` + scope),
- the **rationale** — its description (the *why*, the tradeoffs considered),
- the **validation** — it merged, so it passed review + CI + owner approval, and
- the **discussion** — resolved review threads capture concerns and their
  resolution.

This fills a gap between the two capture paths we already have:

- the **wiki / Ledger** documents *shipped features* (the current state of the
  world), and
- **`/lesson`** pins *rules* (imperative do/don't).

Neither captures the **per-change decision with its rationale** — "MR !X did Y
because Z, superseding the earlier approach W." That is exactly the knowledge a
future agent working the same area needs, and it is exactly what a merged MR
contains — especially when read **together with its issue**: the issue is the
*intent* ("what we wanted"), the merged MR is the *validated realization* ("what we
actually did — sometimes re-scoping the what because the issue's context was
incomplete"). The delta between intent and realization is often the highest-value,
hardest-won knowledge.

The plumbing is already half-present: the `mr-watcher` detects merges and emits
`mr_merged` agent events, and the memory ingester already runs an LLM extraction
pass (observations → `entity` rows) with an ontology and supersession edges. This
change wires merged MRs into that pipeline as a first-class, provenance-tagged
source — **extracting decisions, not dumping diffs.**

## What Changes

- **Merge-triggered ingestion.** On MR merge, the `mr-watcher` (host) publishes a
  memory event carrying the MR's **title + description, the closing issue(s)
  title + description (as context), and a file-path list** — **never the raw diff
  hunks and never review discussion** (v1). Raw diffs are code, not knowledge
  (huge, noisy for recall); review threads are mostly transient process.

- **Extract from curated prose, not chatter or diffs.** The extraction consumes
  the **MR title + description** (the realized change) with the **closing issue(s)
  title + description as context** (the intent), and produces **atomic `decision`
  / `artifact` / `diagnosis` entities** (`provenance = "mr"`), scoped to the
  vigne/project. It uses an **MR-specific prompt** that captures durable decisions
  + rationale, explicitly captures **re-scope deltas** ("the issue assumed X; the
  change did Y because Z"), pairs issue root-cause with MR fix, **ignores
  transient/process feedback** (TODOs, renames, rebases, style nits, CI,
  approvals), and **returns nothing when there is no durable decision — or in any
  doubt** (a trivial fix → nothing; that is the common, correct outcome, and
  precision is preferred over recall). MR knowledge stays in the entity model
  — atomic facts, **single embedding, no chunking**. A long description is
  *reduced* to its decisions, never chunked as a document.

- **The issue is context, not an independent source.** Entities are grounded in
  the **merged MR** (the validated ground truth); the issue only enriches the
  extraction. Because issue content enters memory *only through its merged MR*,
  abandoned or never-implemented issues are never ingested.

- **Two-pass extraction; review is Phase 2, not v1.** Pass 1 (v1) extracts from
  the issue + MR description. **Pass 2 (Phase 2)** extracts from the **review**,
  but only as a **delta over pass 1** — given pass 1's output, it asks "is there a
  *new* durable decision/constraint not already captured? If not, or in doubt,
  return nothing" — with a heuristic pre-filter (drop bot/marker/"LGTM/tests
  pass/pushed" lines), a why-durable justification per item, and lower confidence
  (`provenance=mr-review`, `manual_edit`-prunable). The delta framing makes
  zero-output the default and counters cheap-model over-eagerness; Gemini
  Flash/Haiku are adequate for it. See MR !364 as the worked example.

- **Always-on human-gated marker.** Independently of Pass 2, a reviewer can mark a
  note (`@memory: <point>`) to route it straight to the existing `/lesson`
  pipeline — the high-precision path for load-bearing points (as a reviewer
  already did by hand on !364).

- **Noise filter.** Mechanical MRs are skipped so recall is not flooded with
  churn: Ledger syncs (`… sync Ledger …`), image/chart bumps (`bump-image`,
  `bump-chart`, version-only), pure reverts, and cuvee accumulation merges. The
  filter is a small, configurable set of title/label/size heuristics.

- **Supersession.** When a new MR's decision replaces an earlier one for the same
  area/subject, the ingester links them with the ontology's `supersedes` edge so
  recall surfaces the current decision and can show what it replaced — instead of
  accumulating contradictory decisions (cf. the `cli-*` add-then-revert saga).

- **Idempotent + scoped.** Ingestion is keyed by `<project>!<iid>` so re-delivery
  or replay is a no-op; entities are scoped to the vigne (repo), which matters for
  multi-repo vignobles (e.g. `misc` watches 5 repos).

- **Host assembles, cloud extracts.** The per-vignoble `aoc daemon`
  (`mr-watcher`) — which already holds the GitLab credentials — fetches the
  issue(s) + MR, applies the noise filter, and **publishes** the assembled text in
  the NATS event. The central **memory service** (cluster) runs the LLM extraction
  + embedding and writes SurrealDB. Same host/cloud split as `/lesson` and
  teaching-episode capture: no new credential surface, extraction cost/latency
  stay central, and merges never block on the LLM.

- **Plays well with editable entities (#194).** MR-sourced entities are
  extraction-sourced, so a human edit sets `manual_edit=true` and is protected
  from being clobbered by a later re-ingestion of the same MR/subject.

## Impact

- **Affected specs:** `mr-knowledge-ingestion` (new capability).
- **Affected code:**
  - `internal/watcher/mrs.go` (**host**) — on merge, fetch closing issue(s) + MR,
    apply the noise filter, and publish the memory event
    (MR title/description + issue(s) title/description + files-path list), keyed by
    `<project>!<iid>`. No LLM, no diff hunks, no discussion (v1).
  - `services/memory/ingester.py` (**cloud/memory service**) — new `mr` source
    handler with an **MR-specific extraction prompt** (durable decisions +
    rationale + re-scope delta; ignore transient; may emit zero) feeding the
    existing extraction + ontology + supersession machinery; `provenance="mr"`;
    idempotent per `<project>!<iid>`.
  - `services/memory/recall_service.py` — surface/label `provenance="mr"` hits
    (e.g. `[decision:mr · scope]`).
  - Ontology / `pinard-core` — ensure `provenance="mr"` and `supersedes` edges are
    represented; no schema change beyond what #194 (`manual_edit`) adds.
- **No new external dependency.** Uses the existing GitLab client, NATS memory
  subjects, and SurrealDB ingester.
- **Depends on:** #193 (merged). **Relates to:** #194 (editable entities /
  `manual_edit` guard).
