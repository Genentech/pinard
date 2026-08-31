---
title: "Persistent Memory"
icon: "🧠"
tag: "mem_*"
summary: "Agents remember across sessions — decisions, fixes, and hard-won lessons persist and replicate, so the estate learns instead of relearning."
hero_image: "/images/photos/feature-memory.jpg"
hero_position: "center"
weight: 10
---

## Agents that don't forget

By default, an AI session starts from nothing and ends the same way — every lesson learned dies with the process. For long-running automation, that's expensive: humans re-teach the same recipe on every run.

Pinard gives its agents **persistent memory**. Through the `mem_*` tools (backed by [Engram](https://github.com/Genentech/pinard)), an agent saves durable observations — a bug fix, an architecture decision, a config gotcha — and recalls them in later sessions. Knowledge accumulates instead of evaporating.

## What gets remembered

- **Decisions & patterns** — why something was done a certain way, conventions to follow
- **Fixes & gotchas** — the recovery recipe for a failure the agent hit before
- **Project context** — durable facts about a repo, scoped per project so nothing bleeds across estates

```
mem_save(title="Deploy model: one shared checkout serves all vignobles",
         type="config", scope="project")
mem_search("how do we deploy bin/pinard to vignobles?")
```

## Replicated, not trapped on one host

Memory is local-first but replicates to a central store — so an agent on your workstation, a remote node, and an HPC job share the same accumulated knowledge. A background flush keeps the cloud copy current; nothing is stranded on the machine that happened to learn it.

## On the horizon — a knowledge graph

The next vintage is a **temporal knowledge graph**: operational facts extracted from agent conversations, with validity windows so stale knowledge expires, and boot-time injection so a fresh worker starts already knowing its pipeline's recipes. Teach once; the estate remembers.

## The cellar's memory

A great cellar is a library of vintages — every year's triumphs and mistakes recorded, so the next harvest builds on the last. Pinard's memory is that cellar book: the estate gets wiser with every season.
