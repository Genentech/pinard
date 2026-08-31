---
okf_version: "0.1"
type: Instructions
title: Wiki Constitution
description: Scope, type vocabulary, folder conventions, and contribution rules.
source: pinard
---

# Wiki Constitution

## Scope

This wiki is a curated, human-readable, git-tracked knowledge base that serializes
the memory graph for this vignoble. It is organized by the same ontology that types
the memory graph.

The global `pinard-wiki` repository holds cross-vignoble knowledge. Per-vignoble
wiki directories (this one) hold tenant-scoped knowledge.

## Type Vocabulary

Every concept file MUST have a `type` field in its YAML frontmatter. The core roles
derived from the ontology are:

| Type | Description |
|------|-------------|
| `task` | A unit of work (issue, ticket, job) |
| `step` | A sub-task or action within a task |
| `verdict` | A conclusion or outcome reached |
| `decision` | An architectural or design decision |
| `gate` | A quality or approval checkpoint |
| `action` | A concrete action taken or to be taken |
| `diagnosis` | Root-cause analysis of a problem |
| `log_pattern` | A recurring log message or error pattern |
| `environment_condition` | A state of the environment affecting work |
| `artifact` | A produced output (file, image, report) |
| `runbook` | Operational procedure |
| `pattern` | A recurring design or code pattern |
| `glossary_term` | A term definition |
| `incident` | An incident or outage record |
| `architecture` | An architectural overview or component description |
| `index` | A directory listing (reserved: `index.md` in each dir) |
| `instructions` | Meta-documentation about the wiki itself |

Unknown types are tolerated and preserved — never discard them.

## Folder Conventions

```
wiki/
  index.md              # bundle root index (reserved)
  INSTRUCTIONS.md       # this file (reserved)
  log.md                # change history (reserved)
  decisions/            # type: decision
  architecture/         # type: architecture
  runbooks/             # type: runbook
  patterns/             # type: pattern
  glossary/             # type: glossary_term
  incidents/            # type: incident
```

Sub-directories may have their own `index.md` (a directory listing for that scope).

## Rules

1. **Path = concept ID.** The file path relative to `wiki/` is the concept's stable
   identifier. Do not rename files without updating all links to them.

2. **Links are typed by ontology edge.** Use the `relations` frontmatter key to
   express typed relationships between concepts:
   ```yaml
   relations:
     - edge: ResolvedBy
       to: actions/some-action
   ```
   Markdown links in the body are for human navigation; `relations` are for
   machine consumption.

3. **Confidence gating.** Curator-generated content with `confidence < 0.7` is
   written with `status: needs_review`. Human review promotes it to `auto_serve`.

4. **Human edits are authoritative.** The curator never overwrites a file that has
   `source: human` or that a human has edited (detected via git blame / content hash
   mismatch). It stages changes alongside, not on top of, human edits.

5. **Open-world, never drop.** Knowledge the ontology cannot yet express is staged
   in the closest matching directory with `status: needs_review` rather than
   discarded.

6. **Structural changes go through PR.** New top-level directories, new types added
   to this vocabulary, and cross-scope elevation (per-vignoble → global) require a
   pull request reviewed by a human.
