## ADDED Requirements

### Requirement: Export Cron Trigger

A periodic job SHALL serialize the current knowledge graph state to git-committed recipe files.

#### Scenario: Scheduled export
- **WHEN** the recipe exporter cron fires (default: every 6 hours, configurable via `MEMORY_EXPORT_INTERVAL`)
- **THEN** it queries Graphiti for all current (non-expired) facts across all `group_id` values in the vignoble
- **AND** it serializes facts per group to `recipes/{group_id}/current.yaml`
- **AND** if the serialized content differs from the existing file, it commits the change

#### Scenario: No-op when unchanged
- **WHEN** the serialized output is identical to the existing file
- **THEN** no commit is created
- **AND** the exporter logs "no changes for {group_id}" and exits cleanly

---

### Requirement: YAML Serialization Format

Recipe files SHALL use a structured YAML format containing entities, edges, and proposed types.

#### Scenario: Serialization structure
- **WHEN** facts are serialized to YAML
- **THEN** the file contains a header comment with `group_id`, `exported_at`, and `fact_count`
- **AND** an `entities` list with `type`, `name`, `attributes`, and `valid_from` per entry
- **AND** an `edges` list with `source`, `edge_type`, `target`, `attributes`, and `valid_from` per entry
- **AND** a `proposed_types` list with learned types having 3+ instances

#### Scenario: Expired facts excluded
- **WHEN** a fact has `valid_until` set to a past timestamp
- **THEN** it is excluded from the export
- **AND** it remains in Graphiti for historical queries but is not served to workers

---

### Requirement: Git Commit Strategy

The recipe exporter SHALL commit changes to the vignoble repository without pushing.

#### Scenario: Commit on change
- **WHEN** the exporter detects changes in the serialized output
- **THEN** it stages all modified files in `recipes/`
- **AND** commits with message `[memory-export] Update recipes for {group_ids_changed}`
- **AND** the commit is NOT pushed automatically

#### Scenario: Multi-group export
- **WHEN** the vignoble has multiple active `group_id` values
- **THEN** each group gets its own file: `recipes/{group_id}/current.yaml`
- **AND** the exporter processes all groups in a single run
- **AND** the commit includes all changed groups

---

### Requirement: Wiki Raw Feed

The recipe exporter SHALL also produce summaries for the wiki curation pipeline.

#### Scenario: New or changed facts published to wiki
- **WHEN** the exporter detects changes in the serialized output for a `group_id`
- **THEN** it writes a summary of new/changed facts to `{vignoble}/.wiki/raw/facts/{date}-{group_id}.md`
- **AND** the summary includes: which entities/edges were added or invalidated, source sessions, and timestamps
- **AND** the wiki curator processes this into runbook or pattern page updates on its next cycle
