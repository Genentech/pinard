---
title: "Issue-Driven Work"
icon: "🎫"
tag: "assign → spawn"
summary: "Assign a GitLab issue to Pinard and a vendangeur spawns itself, does the work, opens an MR, and handles review — hands-free."
hero_image: "/images/photos/feature-issue-workflow.jpg"
hero_position: "center"
weight: 4
---

## From ticket to merge, on its own

You don't have to sit at the conductor's desk to start work. Pinard watches your GitLab project, and when an issue is assigned to it, the daemon **spawns a worker on its own** — no human in the loop to kick things off.

The issue *is* the task. Its title and description become the agent's prompt; its labels steer where the work lands.

## The lifecycle

1. **Assign** — assign an issue to the Pinard user (or create it pre-assigned).
2. **Spawn** — the issue watcher spawns a vendangeur, marks the issue `in-progress`, and posts a note with the run details.
3. **Work** — the agent reads the issue, works in its own branch (aware of the vigne's terroir), and opens an MR that `Closes #<n>`.
4. **Review** — review comments are forwarded back to the same agent, which pushes updates.
5. **Harvest** — CI passes, the MR auto-merges, the issue closes, the session is reaped.

## Labels steer the routing

```
parcelle:<name>     # group the work into a workstream (a parcelle)
target:cuvee/<x>    # land the MR on a cuvée branch instead of the default
```

An explicit `target:` label wins over parcelle and project defaults, so a single issue can direct its MR at a cuvée without any extra setup.

## A backlog that tends itself

The most reliable estate hand is the one you never have to summon. Drop a task in the backlog, hand it over, and come back to a merge request. The harvest starts the moment the work is assigned — not the moment you happen to be watching.
