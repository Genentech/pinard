---
title: "Les Domaines"
tag: "over NATS"
summary: "Distribute agents across machines and compute environments — one conductor, many estates."
hero_image: "/images/photos/feature-domaines.jpg"
hero_position: "center"
weight: 7
---

## One conductor, many estates

A great wine region is not a single vineyard. Burgundy is a mosaic of *domaines* — individual estates, each with its own soil, its own caves, its own craft — unified by a shared geography and a common tradition.

Pinard's **Les Domaines** architecture extends this idea to computation: a single conductor orchestrating agents and workers distributed across multiple machines, clusters, or cloud environments. Each machine is a domaine — its own resources, its own capabilities — but all working toward the same harvest.

## The problem it solves

The first piece is already here: a vendangeur no longer has to run on the conductor's machine. Because every component talks only over NATS, a worker can run on a **remote workstation or an HPC node** and still be spawned, streamed to the browser, and reaped like any local agent (see [Remote Workers](/docs/remote-workers/) in The Craft). Les Domaines is the vision of making that a first-class, self-describing fabric.

Some work simply cannot live on one machine:

- A GPU cluster that must train a model
- An HPC node with access to restricted genomic data
- A cloud environment provisioned for a specific pipeline stage
- An edge device that must run a sensor workload

These are compute environments with their own terroir — hardware constraints, data locality, security boundaries. Agents need to run *there*, not on the conductor's laptop.

## Data Pinard — a vision

The most compelling application of Les Domaines is **Data Pinard**: using Pinard to orchestrate distributed data pipelines across heterogeneous compute environments.

Imagine a genomics pipeline:

```
Conductor (your workstation)
├── Domaine: HPC cluster (SLURM)
│   └── Agent: align reads → BAM files
│   └── Agent: variant calling → VCF
├── Domaine: GPU node
│   └── Agent: deep variant calling → refined VCF
└── Domaine: Cloud (AWS Batch)
    └── Agent: annotation → final report
```

Each agent runs where the data and compute live. The conductor dispatches tasks, monitors progress, handles failures, and assembles the final result — the récolte — from the outputs of every domaine.

The agents aren't writing code. They're running pipeline stages. But the orchestration is identical: proposals, tasks, cuvée coordination, harvest monitoring. The same conductor. A different ensemble.

## The architecture

Les Domaines builds on NATS — the messaging layer already at the heart of Pinard — to connect conductors and agents across network boundaries:

```
[Conductor] ──NATS──▶ [Domaine: HPC]
                            └── spawn_agent() → SLURM job
                            └── aoc_notify() → back to conductor

[Conductor] ──NATS──▶ [Domaine: GPU node]
                            └── spawn_agent() → local agent
                            └── aoc_notify() → back to conductor
```

Each domaine runs a lightweight Pinard relay — a `pinard-domaine` daemon that:
- Subscribes to the conductor's NATS subjects
- Spawns agents locally in the domaine's environment
- Forwards agent notifications back to the conductor
- Reports domaine health and capacity

The conductor sees all domaines as vignes in the vignoble — the same `spawn_agent()` call, just targeting a different environment.

## Terroir of compute

Just as `VIGNE.md` captures the terroir of a code repository, each domaine declares its capabilities:

```yaml
# domaine.yaml — HPC cluster
name: hpc-cluster
environment: slurm
resources:
  cores: 1024
  memory: 4TB
  gpu: false
data_access:
  - /data/genomics/restricted
  - s3://internal-bucket
constraints:
  - no internet access
  - max_walltime: 48h
```

The conductor knows what each domaine can do. When it dispatches a task, it routes it to the right terroir — the one with the right soil for that particular grape.

## Status

Les Domaines is **emerging** — the foundations have shipped, the fabric is still being woven:

- [x] NATS messaging foundation across network boundaries
- [x] Remote / HPC workers — spawn, stream, and reap agents on other hosts
- [x] Semi-deterministic **processes** for pipeline steps (see [Processes](/features/processes/))
- [ ] Self-describing domaine registration and health/capacity reporting
- [ ] Domaine-aware proposals and task routing (right terroir for each task)
- [ ] Data Pinard pipeline primitives across heterogeneous compute

If this use case resonates with you, watch the [Pinard repository](https://github.com/Genentech/pinard) for updates.
