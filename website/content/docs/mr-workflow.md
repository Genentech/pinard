---
title: "MR Workflow"
weight: 41
group: Applications
---

# MR Workflow

Pinard's daemon watches every tracked merge request and drives it through a lifecycle
— from CI monitoring, through automated review by the owning maître, to optional
auto-merge.

## Lifecycle overview

```
vendangeur opens MR
       │
       ▼
daemon tracks MR (mr-watcher.yaml)
       │
       ├─ CI fails ──────────────────────► pipeline_failed → vendangeur inbox
       │
       ├─ CI passes + not draft
       │       │
       │       └─ auto_review: true (default)
       │               │
       │               └─► needs_review notification → owning maître
       │                         │
       │                         └─► maître reviews diff, posts signed comment
       │                              (human owner / forge rules handle approval)
       │
       └─ auto_merge: true + approved + no unresolved threads
               │
               └─► daemon merges MR
```

## Automated review (`auto_review`)

Every green, non-draft MR is reviewed by the **maître that owns the workstream**
(the parcelle the vendangeur belongs to). This gives the operator confidence that a
Pinard maître looked at every change — whether or not the vigne is set to auto-merge.

### How it works

1. The daemon detects CI success on a tracked MR.
2. It checks whether the MR HEAD SHA has already been reviewed. If not, it sends a
   `needs_review` notification to the owning parcelle's maître via NATS.
3. The maître receives the notification and triggers a review turn:
   - Reads the changed files with `aoc pressoir get-pr-changes`.
   - Reads existing review comments with `aoc pressoir list-pr-notes`.
   - Posts a signed review comment (signature: `🍇 Reviewed by the <parcelle> maître`).
4. The daemon records the reviewed HEAD SHA. If the vendangeur pushes new commits
   and CI goes green again, a fresh review is triggered automatically.

### Approval policy

**Pinard does not approve MRs automatically.** Approval is the human owner's or
forge's responsibility:

- If the project **requires** approval: a human approves; Pinard's review comment
  provides the signal that a maître looked at the change, but does not satisfy the
  approval gate.
- If the project does **not** require approval: where `auto_merge` is on, the merge
  proceeds because the forge permits it (enforced forge-side via branch-protection
  rules) — no Pinard approval needed.

`aoc pressoir approve-pr` is available as a **manual** CLI tool for operators acting
on explicit instruction, but is never invoked automatically by Pinard.

> **Future path:** if a project is configured to accept a dedicated Pinard bot
> identity (distinct from the operator token) as a trusted approver, Pinard could
> satisfy an approval gate using that bot's token. This is a forge-configuration
> decision, not a Pinard default.

### Idempotency

The watcher records the `reviewed_sha` in `mr-watcher.yaml`. A new review is only
triggered when the MR's HEAD SHA changes — i.e., after a new push. This prevents
duplicate notifications on every watcher tick.

### Noise filter

Trivial and mechanical MRs are skipped automatically: docs sync (`sync Ledger`),
image/chart bumps (`bump-image`, `bump-chart`), and reverts do not trigger a full
maître review turn.

### Opting out

Set `auto_review: false` in `vignes.yaml` to skip automated review for a vigne or
the entire vignoble:

```yaml
# Vignoble-level opt-out (all vignes)
auto_review: false

vignes:
  my-project:
    auto_review: false  # Per-vigne opt-out
```

The default is `true` — review is enabled for all vignes unless explicitly disabled.

## Auto-merge (`auto_merge`)

Auto-merge is independent of auto-review. When `auto_merge: true`, the daemon merges
the MR once:

- CI is green
- The MR is approved (by a human, or the forge has no approval requirement)
- No unresolved reviewer threads remain

With `auto_review: true` and `auto_merge: false` (the default for most vignobles),
the human merges after seeing the maître's review comment.

## CI pipeline failures

When CI fails, the daemon dispatches a `pipeline_failed` event directly to the
vendangeur's inbox so it can investigate and fix the issue. The daemon applies a
circuit breaker after 5 consecutive failures and stops dispatching to prevent an
infinite loop.

## Post-merge monitoring

When `monitor_post_merge: true` (the default), the daemon watches the main branch
pipeline triggered by the merge commit. On success, the worker is reaped. On failure,
a `main_pipeline_failed` event is dispatched to the vendangeur's inbox.
