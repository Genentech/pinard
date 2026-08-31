---
title: "Cuvée Branching"
icon: "🍾"
tag: "cuvee/<name>"
summary: "Batch concurrent MRs through an intermediate branch when agents target the same repository."
hero_image: "/images/photos/feature-cuvee-branching.jpg"
hero_position: "center"
weight: 3
---

## The problem with concurrent agents

When two agents work on the same repository at the same time, they create two merge requests targeting `main`. If both are approved and merged, the second merge may conflict with the first — or worse, overwrite it silently.

This is the multi-barrel problem. You can't pour two fermenting barrels into the same vat at the same time.

## The cuvée solution

Pinard solves this with the **cuvée strategy**: instead of merging directly to `main`, all agents targeting the same repository route their work through a shared intermediate branch — the `cuvée`.

```
main
 └── cuvee/health-checks          ← intermediate branch
       ├── feat/health-api         ← agent 1 MR
       ├── feat/health-worker      ← agent 2 MR
       └── feat/health-auth        ← agent 3 MR
```

Agents merge into the cuvée branch in sequence. The cuvée is reviewed, tested, and merged to `main` as a single unit — the **assemblage**.

## How to use it

When spawning multiple agents on the same repository, create a cuvée first:

```
create_cuvee(project="api-service", name="health-checks")
```

Then spawn each agent with `target_branch="cuvee/health-checks"`:

```
spawn_agent(
  project="api-service",
  prompt="Add /health to the API service",
  target_branch="cuvee/health-checks"
)
spawn_agent(
  project="api-service",
  prompt="Add /health to the worker service",
  target_branch="cuvee/health-checks"
)
```

When all agents finish, open the cuvée MR to merge the full vintage:

```
open_cuvee_mr(
  project="api-service",
  branch="cuvee/health-checks",
  title="Add health-check endpoints across all services"
)
```

## The art of the blend

In winemaking, a cuvée is not a compromise — it is the art of deliberate assembly. The winemaker chooses which barrels to blend, in what proportion, and in what order. The result is greater than any individual barrel.

Pinard's cuvée branching applies that discipline to code: individual agents contribute their barrels, and the conductor decides when the vintage is ready for the final blend.
