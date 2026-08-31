---
title: The SWE Process (GitLab)
weight: 40
group: Applications
---

The SWE process is Pinard's **reference loop** — a
[semi-deterministic loop](/docs/semi-deterministic-loop/) that turns a GitLab issue
into a merged merge request: *issue → change → MR → review → merge → reap*. It is a
complete, batteries-included application built on the engine — and a template for
loops of your own.

> This is **one process**, not the whole of Pinard. If your fleet does something
> other than code review, you write a different [babysitter
> process](/docs/authoring-processes/); everything below is how this particular loop
> is wired.

## Setup — the Pinard GitLab account

The SWE loop acts through a dedicated GitLab service account (role: **Developer**).

| Action | API | Role |
|--------|-----|------|
| Push branches | SSH | Developer |
| Open / merge / comment on MRs | `/merge_requests`, `/notes` | Developer |
| Create issues, read pipelines | `/issues`, `/pipelines` | Developer |

- **Personal access token** — scopes `api`, `read_repository`, `write_repository`.
- **SSH key** — `ssh-keygen -t ed25519 -C "pinard" -f ~/.ssh/pinard_id_ed25519 -N ""`,
  then add the public key to the account.
- **Branch protection** — the default branch should allow "Developers + Maintainers"
  to merge, otherwise Pinard can't [auto-merge](#auto-merge-optional).

## Trigger — assign an issue

The daemon's **issue watcher** scans every vigne for open issues **assigned to the
Pinard user**. Assignment *is* the request — there is no opt-in label and no
confirmation step. When it finds one, it spawns a vendangeur with the issue as
context and labels the issue `in-progress`.

### Owner gate (security)

Pinard only spawns vendangeurs for work the **vignoble owner** has authorized. This
prevents any GitLab user with push access from spending your LLM quota by assigning
issues to the Pinard bot.

An issue passes the gate when **either** is true:

- The issue **author is the owner** (you created it yourself).
- The **owner explicitly approves**: leave a comment on the issue @-mentioning the
  Pinard user with an approval keyword (`approve`, `approved`, or `go`), **or** assign
  the Pinard user to the issue yourself (a system assignment note authored by the owner
  counts as approval).

When an issue is assigned to Pinard but does not yet pass the gate, the watcher:

1. Labels the issue `pinard:awaiting-approval`.
2. Posts a comment on the issue explaining what is needed.
3. Polls each cycle — as soon as the owner approves, the vendangeur spawns.

The gate is **fail-closed**: if `owner` is not configured in `credentials.yaml`, no
auto-spawning happens at all.

#### Configuring the gate

Set `owner` in `credentials.yaml` to the GitLab username of the vignoble operator
(typically your own account):

```yaml
nats:
  user: lelongs   # this becomes the owner automatically
```

The `nats.user` field is used as the owner by default. If you need to specify a
different owner (e.g. when the NATS user and GitLab owner differ), override it with
`owner:` in the credentials file.

To allow the conductor's `spawn_agent` tool to assign issues *as the owner* (so that
assignment itself counts as approval), also configure `owner_token_env`:

```yaml
gitlab:
  owner_token_env: PINARD_OWNER_GITLAB_TOKEN   # human operator's PAT
```

When `PINARD_OWNER_GITLAB_TOKEN` is set, the conductor uses it for issue assignment
(the system note is then authored by *you*, not the bot), and Pinard can auto-approve
its own spawns without requiring a separate approval comment.

> **Security note.** The owner token is **only** emitted for the conductor role
> (`aoc env-exports --role conductor`). Workers receive the bot token only and can
> never hold the operator's GitLab PAT — even if it is in the daemon's environment,
> `aoc spawn` passes workers an explicit, allowlisted environment.

### Labels that gate spawning

| Label | Effect |
|-------|--------|
| `blocked` | Skipped — no vendangeur spawned |
| `pinard:discarded` | Skipped; if already spawned, its state resets so it can retry |
| `pinard:awaiting-approval` | Held — owner hasn't approved yet; watcher re-checks each cycle. **Removed automatically** when the owner approves. |
| `capsule:awaiting-funding` | Capsule-gated — contract detected but not yet funded; see [Buddy Capsules](/docs/capsules/) |

**Retry:** add `pinard:discarded`, then remove it and re-assign — the watcher picks
it up next cycle.

### Labels that route the work

| Label | Effect |
|-------|--------|
| `parcelle:<name>` | Route into a [parcelle](/docs/orchestration/) (else the vigne's own bucket) |
| `target:<branch>` | Target a branch, e.g. `target:cuvee/data-service` (may contain `/`) |

A parcelle can also claim an issue via its `parcelle.yaml`, which may set a
`target_branch:` (the [cuvée](/docs/orchestration/) strategy) without any label.

## The loop — issue to MR

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/swe-process-lifecycle.jpg" alt="A sketched software-work lifecycle from an approved request through an isolated worker and merge-request watcher, with review and failure loops, to merge and worker cleanup.">
    <span class="doc-figure-label mustard" style="--x: 13%; --y: 38%;">Owner gate</span>
    <span class="doc-figure-label charcoal" style="--x: 16%; --y: 57%;">Vendangeur</span>
    <span class="doc-figure-label mustard" style="--x: 38%; --y: 41%;">MR handoff</span>
    <span class="doc-figure-label charcoal" style="--x: 58%; --y: 65%;">MR watcher</span>
    <span class="doc-figure-label terracotta" style="--x: 84%; --y: 30%;">Review feedback</span>
    <span class="doc-figure-label terracotta" style="--x: 81%; --y: 51%;">Pipeline retry</span>
    <span class="doc-figure-label terracotta" style="--x: 49%; --y: 90%;">Circuit breaker</span>
    <span class="doc-figure-label mustard" style="--x: 78%; --y: 91%;">Merge · post-merge · reap</span>
  </div>
  <figcaption><strong>Issue to merge, with one deterministic caretaker.</strong> The watcher routes review and pipeline failures back to the vendangeur, and owns the successful path through merge and cleanup.</figcaption>
  <ul class="doc-figure-legend" aria-label="SWE process legend">
    <li><span><strong>Owner gate</strong> — assignment becomes work only after owner authorization.</span></li>
    <li><span><strong>Vendangeur → MR handoff</strong> — the isolated worker implements and validates, then <code>track_mr</code> transfers care.</span></li>
    <li><span><strong>MR watcher</strong> — reviews and failed pipelines return to the worker; repeated failure reaches the circuit breaker.</span></li>
    <li><span><strong>Merge · post-merge · reap</strong> — approval and green pipelines lead to merge, final monitoring, and deterministic cleanup.</span></li>
  </ul>
</figure>

| Stage | What happens |
|-------|-------------|
| Detected | Watcher publishes `issues_new` |
| Spawned | Vendangeur created in its own git worktree, issue as context |
| In progress | Issue labelled `in-progress` |
| Working | The loop makes the change, validates it, and opens an MR |
| Tracked | The worker calls `track_mr(mr: N)` so the MR watcher takes over |

Comments on a tracked issue are forwarded to the conductor as `issues_comment`
events (Pinard's own comments are filtered, so there's no feedback loop).

## The MR watcher

Once an MR is tracked (written to `.state/mr-watcher.yaml`, polled every ~30s), the
daemon tends it through its whole lifecycle:

```
Worker opens MR → track_mr → MR watcher
    ├── Review comments → forwarded to the worker (threaded via discussion_id)
    ├── Pipeline fails   → dispatched to the worker (attempt X/5)
    ├── Pipeline passes  → informational event to the conductor
    ├── Approved + green  → auto-merge (only if enabled — off by default)
    ├── Merged (human or auto) → post-merge pipeline + tag monitoring
    ├── Circuit breaker (5 failures) → worker killed
    └── Terminal condition → reapWorker (the single teardown point)
```

### Review forwarding

New reviewer notes are published as `review_comment` events carrying
`discussion_id` (for threaded replies) and `file`/`line` (for inline comments), and
dispatched straight to the worker's inbox — including the exact
`glab api …/discussions/<id>/notes` command to reply in-thread.

### Auto-merge (optional)

**Off by default — a human merges.** When enabled via `auto_merge: true` in
[`vignes.yaml`](/docs/configuration/) (per-vigne or global), the watcher merges once
**all** hold: pipeline **success**, at least one **approval**, **no unresolved
threads**, and **not a Draft**. If unapproved, a `needs_approval` event goes to the
conductor. With auto-merge off, none of this runs.

### Post-merge & reap

After merge, the watcher monitors the main and tag pipelines (reporting to the
conductor), and finally calls **`reapWorker`** — the single, deterministic teardown
point that kills the tmux session and cleans up the worktree.

## See also

- **[Authoring Processes](/docs/authoring-processes/)** — write your own loop.
- **[Orchestration & Parcelles](/docs/orchestration/)** — group SWE work into workstreams.
- **[Configuration](/docs/configuration/)** — `auto_merge` and other toggles.
- **[Buddy Capsules](/docs/capsules/)** — let a colleague fund a vendangeur's quota.
