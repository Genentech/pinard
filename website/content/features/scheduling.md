---
title: "Scheduled Harvests"
icon: "⏰"
tag: "schedules.yaml"
summary: "Cron-driven agent spawns for nightly linting, weekly dependency updates, daily security audits."
hero_image: "/images/photos/feature-scheduling.jpg"
hero_position: "center"
weight: 4
---

## Automating the harvest

Not every change needs a conductor at the keyboard. Some work is periodic, predictable, and mechanical — exactly the kind of task that should happen while you sleep.

Pinard's **scheduler** lets you define cron-driven agent spawns. At the scheduled time, Pinard spawns an agent with your prompt, and the agent does the work, opens an MR, and waits for review.

## Defining schedules

Schedules live in `schedules.yaml` at the root of your vignoble:

```yaml
schedules:
  nightly-lint:
    project: api-service
    cron: "0 2 * * *"        # 2am every night
    prompt: >
      Run the linter on the entire codebase. Fix any auto-fixable
      issues. Open an MR with the changes.

  weekly-deps:
    project: api-service
    cron: "0 9 * * 1"        # Monday 9am
    prompt: >
      Update all Python dependencies to their latest compatible
      versions. Run tests to confirm nothing breaks. Open an MR.

  daily-security:
    project: auth-service
    cron: "0 6 * * *"        # 6am every day
    prompt: >
      Run bandit and safety checks. If any high-severity issues
      are found, open an MR with fixes. Otherwise just report.
```

## Use cases

**Nightly maintenance**
- Fix lint errors before they accumulate
- Update generated files (OpenAPI specs, protobuf stubs)
- Rotate test fixtures

**Weekly hygiene**
- Dependency updates (pip, npm, cargo)
- Remove dead code flagged by coverage tools
- Sync documentation with code changes

**On-demand triggers**
Set `once: true` to run a schedule exactly once — useful for one-off migrations or bootstrapping tasks that don't need to repeat.

## The midnight harvest

The best estates don't stop working when the winemaker goes home. Fermentation continues, barrels age, and the cellar breathes on its own schedule.

Pinard's scheduler is that cellar: it tends your codebase through the night, so you arrive each morning to a cleaner estate.
