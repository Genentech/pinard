---
title: "Parcelles & the Crew"
icon: "🗂️"
tag: "parcelle.yaml"
summary: "Group related work into workstreams, each with its own conductor — a three-tier harvest crew that scales one estate to many parallel efforts."
hero_image: "/images/photos/feature-parcelles.jpg"
hero_position: "center"
weight: 5
---

## One estate, many rows

A real vineyard is divided into **parcelles** — distinct plots, each worked on its own rhythm, all part of one estate. Pinard borrows the word for a **workstream**: a named grouping of related issues, workers, and a target branch, tended by its own conductor.

A parcelle is defined by a file at `parcelles/<name>/parcelle.yaml`:

```yaml
name: data-pipeline
project: genomics
status: active
description: "Nightly genome build + QC"
target_branch: cuvee/data-pipeline   # cuvée routing for this workstream
issues: [42, 43, 44]                  # issues that belong here
spec: openspec/specs/data-pipeline/parcelle.md
```

The daemon uses the `issues` list (and `parcelle:<name>` labels) to route work, and `target_branch` to batch each parcelle's MRs through its own cuvée.

## The harvest crew — three tiers

Pinard isn't one agent; it's a crew, each with a role:

```
Régisseur   — the estate manager: the general lane, overall coordination
  └── Maître   — a cellar master per parcelle: conducts one workstream
        └── Vendangeur — the harvester: a worker doing one task
```

- **Régisseur** — the top-level conductor for the whole vignoble.
- **Maître** — a per-parcelle conductor window; open or focus one on demand.
- **Vendangeur** — an individual worker (agent) on a single task.

```
attach_parcelle("data-pipeline")   # spawn/focus this parcelle's maître
```

## Why it scales

Isolating work into parcelles means many efforts run in parallel without stepping on each other: each has its own cuvée branch, its own conductor, its own set of issues, and its own view in the control room and dashboard. Add a parcelle, and the estate grows another row — no reorganization required.

## Terroir at every level

From the estate manager down to the harvester, everyone knows the land they work. Parcelles give that structure to your automation: distinct plots, one vineyard, a single harvest.
