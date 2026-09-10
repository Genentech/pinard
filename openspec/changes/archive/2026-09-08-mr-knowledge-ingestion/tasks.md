# Tasks: mr-knowledge-ingestion

## 1. Merge-trigger + event assembly — HOST (`internal/watcher/mrs.go`)
- [ ] On the merged transition, assemble an `mr` memory payload:
      MR title + description, **closing issue(s) title + description** (context;
      all issues the MR closes), `files_changed` (file paths only — **no diff
      hunks**), `merged_at`, author, `project`, `iid`, `scope`. **No review
      discussion** (v1).
- [ ] Fetch via the existing GitLab client: MR; MR changes → file paths only;
      closing issues via the API's closing-issues list (fall back to parsing
      `Closes #N`).
- [ ] Publish to `pinard.<vignoble>.memory.mr` (new subject alongside
      `…memory.rules`); idempotent — include `<project>!<iid>` as the dedup key.
- [ ] No LLM and no SurrealDB on the host; extraction happens in the cloud
      ingester (see §3). Fail-open: any assembly/fetch/publish error logs and
      drops just this MR.

## 2. Noise filter (`internal/watcher/mrs.go`)
- [ ] Skip mechanical MRs before publishing: `sync Ledger`, `bump-image`,
      `bump-chart`, version-only, `^Revert `, cuvee-source-branch merges,
      empty/template descriptions.
- [ ] Honour labels: `memory:skip` (never ingest), `memory:capture` (always
      ingest, bypass heuristics).
- [ ] Make heuristics configurable (config or constants); conservative by design
      (prefer letting a noise MR through over dropping a real decision).
- [ ] Unit tests for the filter (positive/negative cases incl. label overrides).

## 3. Ingester `mr` source handler — CLOUD/memory service (`services/memory/ingester.py`)
- [ ] Subscribe/consume `…memory.mr`; route to a new `_handle_mr` path.
- [ ] Feed **MR title + description** (realized) with **closing issue(s)
      title + description as context** (intent) to the LLM extraction using an
      **MR-specific prompt** (NOT the generic ontology prompt): emit durable
      `decision`/`artifact`/`diagnosis` entities; capture **re-scope deltas**
      (issue intent vs MR realization) and issue-cause→MR-fix; **ignore**
      transient/process content (TODOs, "forgot X", renames, rebases, nits, CI,
      approvals); **return nothing when there is no durable decision — or in any
      doubt** (trivial change → write nothing, treated as success; precision over
      recall).
- [ ] Entities grounded in the merged MR (issue is context only — do not emit
      "what we wanted" as fact).
- [ ] Set `provenance="mr"`, `scope`=vigne, `data={mr:"<project>!<iid>",
      issues:[…], url, merged_at, files_changed}`; single embedding per entity
      (**no chunking**).
- [ ] Idempotent per `<project>!<iid>` (processed marker / cursor); re-delivery
      is a no-op.
- [ ] LLM-unavailable handling mirrors teaching-episode extraction (defer/skip,
      never crash).

## 4. Supersession (`services/memory/ingester.py` + ontology)
- [ ] When an MR decision maps to an existing subject `(role,name)` or the LLM
      flags it as replacing a prior decision, create a `supersedes` edge
      (new → old).
- [ ] Revert MRs (when ingested via `memory:capture`) supersede the decision they
      revert.
- [ ] Recall prefers newest; `/recall` can display "(supersedes: …)".

## 5. Recall surfacing (`services/memory/recall_service.py`)
- [ ] Label `provenance="mr"` hits distinctly (e.g. `[decision:mr · scope]`).
- [ ] Ensure `files_changed`/`url` from `data` are available on `/recall fetch`
      but excluded from the embedded text.

## 6. #194 interaction
- [ ] Confirm MR-sourced entities respect the `manual_edit` guard (a hand-edited
      MR-derived entity survives re-ingestion of the same MR).

## 7. Tests + docs
- [ ] Ingester tests: mechanical MR skipped; real MR → decision entity with
      `provenance=mr`; **re-scope delta captured** when MR diverges from issue;
      **trivial MR → zero entities** (success, no write); re-ingestion idempotent;
      supersedes edge on replacement.
- [ ] Recall test: `mr` hits labelled; diff/files excluded from embedding.
- [ ] Confirm review discussion is NOT ingested in v1.
- [ ] Ledger/docs: document the new capture source under memory-architecture /
      memory-curation.

## 8. Rollout
- [ ] Forward-only from deploy (no retro backfill in v1).
- [ ] Optional, bounded, filtered historical backfill as a follow-up if useful.

## 9. Phase 2 — review extraction (delta pass; not v1)
- [ ] Extend the host event (or a follow-up event) to carry pre-filtered review
      notes: drop bot notes, `pinard:*` markers, and pure "LGTM/tests pass/
      pushed/rebase/CI" lines.
- [ ] Ingester Pass 2: run **after** Pass 1, given Pass 1's entities as context;
      extract only **net-new** durable decisions/constraints; "if not, or in
      doubt, return nothing"; require a why-durable justification per item.
- [ ] Tag Pass-2 entities `provenance="mr-review"` with lower confidence;
      `manual_edit`-prunable (#194).
- [ ] Tests: review with a signposted constraint (cf. !364) → net-new entity;
      noise-only review → zero entities; item already in Pass 1 → not duplicated.

## 10. Always-on — human-gated `@memory:` marker (can ship independently)
- [ ] A review note starting with `@memory:` (or a note-level `memory:capture`)
      routes that text to the existing `/lesson` pipeline — high-precision, no
      classification. Host detects the marker; no LLM classification needed.
