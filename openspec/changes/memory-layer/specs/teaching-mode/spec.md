## ADDED Requirements

### Requirement: Teaching Mode Activation

The conductor SHALL provide a `/teaching` command that signals aggressive knowledge extraction mode. Teaching mode is an optional emphasis signal, not a gate — transcripts are always ingested regardless.

#### Scenario: Activating teaching mode
- **WHEN** the human types `/teaching` or `/teaching on` in the conductor
- **THEN** the conductor sets an internal `teachingMode = true` flag
- **AND** publishes to `pinard.{vignoble}.memory.teaching.{conductor_session}` with `{"active": true}`
- **AND** subsequent conductor-originated messages are published with `mode: "teaching"`
- **AND** worker messages for the connected session are also tagged `mode: "teaching"`

#### Scenario: Deactivating teaching mode
- **WHEN** the human types `/teaching off` or `/teaching stop`
- **THEN** `teachingMode` is set to `false`
- **AND** subsequent messages revert to `mode: "normal"`

#### Scenario: Teaching mode is not a gate
- **WHEN** teaching mode is not active
- **THEN** worker transcripts are still published to `memory.episodes` with `mode: "normal"`
- **AND** extraction still occurs but conservatively (prescribed types only)

---

### Requirement: Retroactive Capture

The conductor SHALL support retroactively marking prior conversation turns as teaching material.

#### Scenario: Retroactive capture with --from
- **WHEN** the human types `/teaching --from 30m`
- **THEN** the conductor retrieves the last 30 minutes of conversation from its turn history
- **AND** publishes those turns as a single episode with `mode: "teaching"` and original timestamps
- **AND** teaching mode is activated for subsequent turns

#### Scenario: Full session capture with --all
- **WHEN** the human types `/teaching --all`
- **THEN** all turns from the current conductor session are published as a teaching episode
- **AND** timestamps and ordering are preserved
- **AND** teaching mode is activated for subsequent turns

---

### Requirement: Session-End Bulk Publish

Teaching sessions SHALL be published as a complete transcript when the session ends, providing better extraction context than per-turn episodes.

#### Scenario: Bulk publish on disconnect
- **WHEN** the human disconnects from a worker and teaching mode was active during the session
- **THEN** the full teaching-mode transcript is published as a single episode to `pinard.{vignoble}.memory.episodes`
- **AND** `episode.content` contains the concatenated turns with role prefixes
- **AND** `mode` is `"teaching"`

#### Scenario: Normal mode does not bulk publish
- **WHEN** the session ends and teaching mode was never activated
- **THEN** no bulk episode is published (per-turn episodes already handled normal ingestion)
