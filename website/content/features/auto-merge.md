---
title: "Auto-Merge & Watch"
icon: "🔀"
tag: "auto_merge: true"
summary: "MRs merge when approved and CI passes. Post-merge pipelines monitored for failures."
hero_image: "/images/photos/feature-auto-merge.jpg"
hero_position: "center"
weight: 5
---

## From open to merged, hands-free

Once an agent opens a merge request, the work isn't over — it has to land. Waiting for CI to pass, watching for approvals, clicking the merge button: these are mechanical steps that shouldn't require human attention.

Pinard's **MR watcher** automates the entire post-open lifecycle.

## How auto-merge works

When `auto_merge: true` is set for a vigne in `vignes.yaml`, Pinard monitors every MR opened by an agent on that repo:

1. **CI monitoring** — watches pipeline status in real time
2. **Approval detection** — waits for the required approvals
3. **Auto-merge trigger** — merges the MR when all conditions are met
4. **Post-merge watch** — monitors the pipeline that runs after merge

If CI fails, the watcher flags it and routes the failure back to the conductor. If a post-merge pipeline breaks, you're notified immediately — not hours later.

## Configuration

```yaml
# vignes.yaml
vignes:
  api-service:
    path: ~/projects/api-service
    repo: my-team/api-service
    auto_merge: true          # enable auto-merge for agent MRs
```

You can also track MRs that weren't opened by agents:

```
track_mr(project="api-service", mr=42)
```

## The watcher's vigil

The MR watcher runs as a background service alongside Pinard. It polls GitLab for status changes on all tracked MRs, and delivers events to the conductor's session via NATS.

Events include:
- MR approved
- CI passed or failed
- MR merged
- Post-merge pipeline status
- New review comments (forwarded to the agent)

## Patience and precision

A winemaker doesn't hover over every barrel. They set the conditions — temperature, humidity, time — and let the cellar do its work. They return when it matters.

Pinard's auto-merge is the same discipline: set the conditions (`auto_merge: true`, CI gates, approval rules), and let the system bottle the vintage when it's ready.
