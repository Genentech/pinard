---
title: "OpenSpec Dispatch"
icon: "📐"
tag: "/dispatch"
summary: "Author an OpenSpec change (proposal + task checklist), then turn it into GitLab issues and spawned agents with one command."
hero_image: "/images/photos/feature-openspec-dispatch.jpg"
hero_position: "center"
weight: 1
---

## From a spec to a fleet of agents

Big changes deserve a plan before any agent touches a file. Pinard leans on the
**OpenSpec** convention: you author a *change* in the repo — a `proposal.md` (goals and
rationale) plus a `tasks.md` (a checklist of the work) under
`openspec/changes/<name>/`. Then one conductor command turns that plan into running work.

## `/dispatch <change-name>`

From a conductor (régisseur or maître):

```
/dispatch add-health-endpoints
```

Pinard then:

1. **Finds the change** — scans every vigne's `openspec/changes/` (skipping archived
   ones). With no name, it lists all active changes across your repos so you can pick.
2. **Reads it** — loads the change's `proposal.md` and `tasks.md` and counts the pending
   `- [ ]` tasks.
3. **Plans the dispatch** — groups the tasks into GitLab issues and shows you the
   grouping as a table *first*, for review.
4. **Creates the work** — opens a GitLab issue per group (`aoc issue`, labelled for
   [cuvée](/features/cuvee/) batching) and spawns an agent per task (`aoc spawn`), each
   receiving the proposal as context so it understands the bigger picture — not just its
   slice.

Multi-repo changes stay coordinated through the [cuvée](/features/cuvee/) strategy: the
issues are cuvée-labelled so their agents' MRs batch through an intermediate branch
rather than racing to `main`.

## Why author the spec first

The change is a **shared contract**. Goals and scope are decided — and reviewable — at
the cheapest possible moment, before any code is written. It's the same discipline a
winemaker applies before the blend: you taste, you plan, you decide the assemblage
before pouring a single barrel.

```markdown
# openspec/changes/add-health-endpoints/tasks.md

## 1. Endpoints
- [ ] api-service    — add GET /health returning 200
- [ ] worker-service — add GET /health returning 200
- [ ] auth-service   — add GET /health returning 200
```

> **You** write the OpenSpec change; **Pinard** turns it into issues and agents. It
> doesn't invent the plan for you — it dispatches the one you've committed to the repo.
