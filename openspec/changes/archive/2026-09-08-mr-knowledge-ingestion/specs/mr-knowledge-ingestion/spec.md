## ADDED Requirements

### Requirement: Merged MRs are ingested as knowledge, un-merged MRs are not

The system SHALL ingest a merge request into memory only when it transitions to
**merged**. Open, closed-without-merge, and draft MRs SHALL NOT be ingested, so
that only validated (reviewed + merged) decisions enter memory.

#### Scenario: Merge triggers ingestion
- **WHEN** an MR transitions to merged and passes the noise filter
- **THEN** the watcher publishes an `mr` memory event for that MR
- **AND** the ingester creates one or more `provenance="mr"` entities from it

#### Scenario: Non-merged MRs are ignored
- **WHEN** an MR is opened, updated, or closed without merging
- **THEN** no `mr` memory event is published and no entity is created

### Requirement: Ingestion captures rationale from MR + issue, never raw diffs or discussion

The published `mr` event SHALL contain the MR title + description, the **closing
issue(s) title + description** (as context), and a **file-path-only**
`files_changed` list. It SHALL NOT contain diff hunks or review-discussion text.
Extraction SHALL be grounded in the merged MR (the issue is context only), use an
MR-specific prompt, and reduce the input to atomic `decision`/`artifact`/
`diagnosis` entities; MR content SHALL NOT be chunked.

#### Scenario: Diff hunks and discussion are excluded
- **WHEN** an MR with a large diff and a long review thread is ingested
- **THEN** the event and resulting entities contain no diff hunk or review-thread text
- **AND** `files_changed` holds only file paths, stored in entity `data` (not embedded)

#### Scenario: Description is reduced to atomic decisions
- **WHEN** an MR has a long, multi-section description
- **THEN** the ingester emits atomic `decision`/`artifact` entities (single
  embedding each), not a single large or chunked record

### Requirement: The issue provides intent; re-scope deltas are captured

When an MR closes one or more issues, their title + description SHALL be supplied
to extraction as **context** (intent). Entities SHALL remain grounded in the
merged MR (the validated realization); the extraction SHALL NOT emit an issue's
stated intent as fact. When the change diverges from the issue's intent, the
divergence SHALL be captured as a decision.

#### Scenario: Re-scope delta is captured
- **WHEN** an MR realizes a goal differently from its issue's stated intent
- **THEN** a `decision` entity records the divergence and its rationale
  ("the issue assumed X; the change did Y because Z")

#### Scenario: Root cause is paired with fix
- **WHEN** the issue states a root cause and the MR states the fix
- **THEN** extraction MAY emit a `diagnosis` entity pairing cause → fix, grounded
  in the merged MR

#### Scenario: Intent is not recorded as fact
- **WHEN** an issue describes a desired behavior that the MR did not implement
- **THEN** that unrealized intent is NOT emitted as an entity

### Requirement: Extraction ignores transient content and defaults to nothing

Extraction SHALL ignore transient/process content (TODOs, "you forgot X",
renames, rebases, style nits, CI, approvals, restatements without rationale) and
SHALL be permitted to emit **zero entities**. When there is no durable decision,
**or when the model is uncertain**, it SHALL return nothing (precision is
preferred over recall). A zero-entity result SHALL be treated as success (nothing
written), not an error.

#### Scenario: Trivial MR yields no entities
- **WHEN** a merged MR contains no durable decision (e.g. a small mechanical fix)
- **THEN** the ingester writes no entities and records the MR as processed

#### Scenario: Uncertainty yields nothing
- **WHEN** the extraction is in doubt whether a candidate is durable
- **THEN** it emits no entity for that candidate

### Requirement: v1 extracts from issue+description; review is a Phase-2 delta pass

v1 extraction (Pass 1) SHALL use only the MR title+description and the closing
issue(s) as context; it SHALL NOT auto-extract from review discussion. Review
extraction (Pass 2) is a later phase that SHALL run only after Pass 1, take Pass
1's output as context, emit only **net-new** durable items not already captured,
return nothing when none or when uncertain, and tag results with lower confidence
(`provenance="mr-review"`). A human-gated `@memory:`-marked note MAY route to the
`/lesson` pipeline independently of either pass.

#### Scenario: v1 does not ingest review discussion
- **WHEN** a merged MR with substantive review threads is ingested under v1
- **THEN** no entity is created from the discussion text (only Pass 1 runs)

#### Scenario: Phase-2 review pass captures only net-new items
- **WHEN** Pass 2 runs on a review whose only durable point is already captured by
  Pass 1
- **THEN** it creates no new entity
- **WHEN** the review contains a new, durable constraint not in Pass 1
- **THEN** it creates one `provenance="mr-review"` entity with lower confidence

### Requirement: Assembly runs on the host; extraction runs in the memory service

The host (`aoc daemon` / `mr-watcher`) SHALL fetch the MR + closing issue(s),
apply the noise filter, and publish the event, performing **no** LLM call and
**no** SurrealDB write. The central memory service ingester SHALL perform the LLM
extraction + embedding + SurrealDB write, using only the event payload (it SHALL
NOT reach back to GitLab).

#### Scenario: Host publishes, cloud extracts
- **WHEN** an MR merges
- **THEN** the host publishes the assembled `mr` event with no LLM/DB work
- **AND** the memory service ingester performs extraction and writes SurrealDB
  from the payload alone

#### Scenario: Merge is not blocked by extraction
- **WHEN** the extraction LLM is slow or unavailable
- **THEN** the merge and watcher loop are unaffected and the event is processed
  when the ingester recovers

### Requirement: Mechanical MRs are filtered out

The watcher SHALL skip mechanical MRs (Ledger syncs, image/chart version bumps,
pure reverts, cuvee accumulation merges, empty/template descriptions) before
publishing an event, and SHALL honour a `memory:skip` label (never ingest) and a
`memory:capture` label (always ingest, bypassing heuristics).

#### Scenario: Ledger-sync / bump MRs are skipped
- **WHEN** a merged MR titled `feat(docs): sync Ledger to <sha>` or a
  `bump-image`/`bump-chart` MR merges
- **THEN** no `mr` memory event is published

#### Scenario: Labels override heuristics
- **WHEN** a merged MR carries `memory:skip`
- **THEN** it is never ingested, even if it would otherwise pass
- **WHEN** a merged MR carries `memory:capture`
- **THEN** it is ingested even if a heuristic would have skipped it

### Requirement: MR entities are provenance-tagged, scoped, and idempotent

Entities created from an MR SHALL carry `provenance="mr"`, be scoped to the MR's
vigne/repo, and reference their source MR in `data`. Ingesting the same MR more
than once SHALL be a no-op.

#### Scenario: Provenance and scope
- **WHEN** an MR in repo R is ingested
- **THEN** its entities have `provenance="mr"`, scope = R's vigne scope, and
  `data.mr == "<project>!<iid>"`

#### Scenario: Re-ingestion is idempotent
- **WHEN** the same merged MR event is delivered or replayed twice
- **THEN** no duplicate entities are created and existing ones are unchanged

#### Scenario: Multi-repo vignoble isolation
- **WHEN** two repos in the same vignoble each have an MR with the same iid
- **THEN** they produce distinct entities keyed by `<project>!<iid>` in their own
  scopes, with no cross-contamination

### Requirement: Superseding decisions are linked, not accumulated

When an MR's decision replaces an earlier decision for the same subject, the
ingester SHALL create a `supersedes` edge (new → old) so recall surfaces the
current decision and can show what it replaced, instead of returning
contradictory decisions.

#### Scenario: A later MR supersedes an earlier decision
- **WHEN** a merged MR establishes a decision on the same subject as an existing
  `provenance="mr"` entity
- **THEN** a `supersedes` edge is created from the new entity to the old
- **AND** recall prefers the newest and can display "(supersedes: …)"

#### Scenario: A revert supersedes what it reverted
- **WHEN** a revert MR is ingested (via `memory:capture`)
- **THEN** it supersedes the decision entity it reverts

### Requirement: Ingestion is fail-open and does not affect merges

Any failure in assembling, publishing, or ingesting an MR event SHALL be logged
and confined to that MR. It SHALL NOT block the merge, the watcher loop, or other
MRs, and SHALL degrade gracefully when the extraction LLM is unavailable.

#### Scenario: LLM or fetch failure is contained
- **WHEN** GitLab fetch, NATS publish, or the extraction LLM fails for one MR
- **THEN** that MR is skipped/deferred with a log line
- **AND** merges and subsequent MR ingestion continue unaffected

### Requirement: MR entities honour manual edits

An MR-sourced entity that a human has edited (`manual_edit=true`, per the editable
-entities capability) SHALL NOT be overwritten by re-ingestion of the same MR or
subject.

#### Scenario: Hand-edited MR entity survives re-ingestion
- **WHEN** a human edits an MR-derived entity and the same MR is re-ingested
- **THEN** the human description and its embedding are preserved
