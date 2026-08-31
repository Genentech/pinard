---
title: "Independent Agents"
icon: "🤖"
tag: "spawn_agent()"
summary: "One Pi agent session per task. Each agent works in isolation, aware of its vigne's terroir via VIGNE.md."
hero_image: "/images/photos/feature-agents.jpg"
hero_position: "center top"
weight: 2
---

## What are Agents?

In Pinard, an **agent** is a single [Pi](https://github.com/Genentech/pinard) session assigned to one task in one repository. Agents are ephemeral, focused, and independent — each one lives and dies with its task.

When Pinard spawns an agent, it:

- Opens a dedicated tmux session
- Clones or reuses the target vigne
- Injects the task prompt, the proposal context, and the vigne's `VIGNE.md`
- Lets the agent work autonomously — reading code, writing changes, running tests, opening MRs

## VIGNE.md — the terroir

Every vigne can have a `VIGNE.md` file at its root. This is the **terroir** of the repository: the invisible hand that shapes how every agent works within it.

```markdown
# VIGNE.md — api-service

## Test commands
- Run: `pytest tests/ -v`
- Lint: `ruff check .`

## Conventions
- Follow PEP 8
- All endpoints must have type hints
- Never modify `config/production.yaml` directly

## Do not touch
- `legacy/` — deprecated code, leave as-is
- `scripts/db-migration.py` — manual migrations only
```

The agent reads this file at the start of every session. It doesn't need to be told the rules — the terroir is already there.

## Isolation by design

Each agent has no knowledge of what other agents are doing. This is intentional. Isolation means:

- No shared state, no race conditions
- Each agent can be retried independently if it fails
- Review feedback reaches the right agent, not all of them
- Sessions are cheap to create and safe to kill

## The conductor's role

You — the conductor — decide when to spawn agents, what to tell them, and when to let them run. Pinard's `spawn_agent()` function is the baton: you raise it, and a new voice joins the ensemble.

```
spawn_agent(
  project="api-service",
  prompt="Add GET /health endpoint returning JSON status",
  target_branch="cuvee/health-checks"
)
```

One conductor. Many agents. One harvest.
