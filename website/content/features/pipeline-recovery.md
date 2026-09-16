---
title: "Pipeline Retry & Recovery"
icon: "↻"
tag: "attempt X/5"
summary: "Failed MR pipelines return to the same vendangeur for a bounded repair loop. Repeated failure trips the circuit breaker."
hero_image: "/images/photos/feature-pipeline-recovery.jpg"
hero_position: "center"
weight: 7
---

## A failed pipeline is feedback

An agent does not disappear after opening a merge request. Once it calls
`track_mr`, Pinard's **MR watcher** keeps the MR connected to the vendangeur that
created it. If CI fails, the watcher sends the failure back to that worker with the
pipeline URL and a bounded attempt count.

The worker fixes the cause, pushes a new commit, and lets GitLab start a **new**
pipeline. Pinard does not repeatedly rerun the same failing pipeline and hope for a
different result.

## The repair loop

```text
new failed pipeline
        ↓
record its pipeline ID and increment the failure count
        ↓
send pipeline_failed to the originating worker (attempt X/5)
        ↓
worker diagnoses, fixes, and pushes
        ↓
new pipeline ── success → reset the failure count to zero
             └─ failure → another bounded repair attempt
```

Each GitLab pipeline ID is handled once. Repeated watcher polls therefore do not
inflate the counter, and the count is persisted in `.state/mr-watcher.yaml` so a
daemon restart does not erase the history.

The worker receives a direct instruction of the form:

```text
CI pipeline failed on MR !42 (attempt 3/5).
See: <pipeline URL>. Fix the failing job and push.
```

For process-backed workers, the event remains actionable even if the local session
is temporarily gone: Pinard's recovery machinery can restore the process worker and
continue from its persisted run state.

## The circuit breaker

Pinard allows **five repair attempts**. Failures 1 through 5 are returned to the
worker as `pipeline_failed`. If the next distinct pipeline also fails, the watcher:

1. publishes a `circuit_breaker` event with the MR, project, and failure count;
2. stops the worker session; and
3. leaves the tracked MR state available for inspection and manual intervention.

The breaker is deliberately conservative. It does not auto-merge, auto-revert, or
silently discard the branch. Its job is to stop an unproductive repair loop before
it consumes more time and model quota.

## What resets the count?

A newly observed **successful MR pipeline** resets `pipeline_fail_count` to zero and
publishes `pipeline_passed`. A transient GitLab API error, an unchanged pipeline ID,
or the absence of a pipeline changes nothing.

This means the limit measures consecutive failed repair cycles, not the lifetime
number of failures on an MR.

## Scope

This loop applies to the **pre-merge pipelines of a tracked merge request**.
Post-merge main and tag pipelines are monitored separately: those failures are also
routed to the worker, but they do not use this five-attempt counter.

The full issue-to-merge lifecycle, including review forwarding, optional auto-merge,
post-merge monitoring, and deterministic cleanup, is documented in
[The SWE Process](/docs/swe-process/).

## Recover, then continue

A failed test is not terminal; an endless retry loop is. Pinard keeps the useful
middle ground: return concrete CI feedback to the agent that owns the change, retain
enough state to survive interruptions, and stop decisively when repeated repair is
no longer productive.
