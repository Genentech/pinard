## ADDED Requirements

### Requirement: Credential bootstrap (uncork)
`aoc` SHALL provide an `uncork` subcommand that materializes a credential/config
bundle for a sandboxed worker. It SHALL accept the bundle from a URL
(`--url`, default `$PINARD_UNCORK_URL`) or stdin, expect a JSON manifest of the
form `{ "files": [ { "path", "mode"?, "content", "encoding"? } ] }`, and write
each file under `$HOME` (paths relative to `$HOME`) with the declared mode
(default `0600`), creating parent directories.

#### Scenario: Materialize a bundle from a URL
- **WHEN** `aoc uncork --url <endpoint>` is run and the endpoint returns a valid
  manifest
- **THEN** each listed file is written under `$HOME` with its mode
- **AND** the command exits zero

#### Scenario: Fail fast on a bad bundle
- **WHEN** the endpoint returns a non-2xx status or malformed JSON
- **THEN** `aoc uncork` prints an error and exits non-zero without writing partial
  secrets that would be used as complete

### Requirement: Proxy provider seeding (ensure-proxy-provider)
`aoc ensure-proxy-provider` SHALL make pi aware of the LLM proxy provider in a
`--containall` sandbox whose `~/.pi` starts empty. It SHALL build the provider
registry (`~/.pi/agent/models.json`) from image-baked non-secret defaults (base
URL, headers, model ids) and mint the provider credential (`~/.pi/agent/auth.json`)
from `PINARD_POUR_URL`. When `~/.claude/settings.json` is present, it MAY continue
to source the provider from that file (backward compatible). It SHALL remain
idempotent: if `models.json` already defines a proxy provider, it is left as-is.

#### Scenario: Seed from baked defaults + pour URL
- **WHEN** `aoc ensure-proxy-provider` runs with `PINARD_POUR_URL` set and no
  `~/.claude/settings.json`
- **THEN** `~/.pi/agent/models.json` is written from baked defaults
- **AND** `~/.pi/agent/auth.json` contains a token minted from `PINARD_POUR_URL`

#### Scenario: Backward-compatible settings.json path
- **WHEN** `~/.claude/settings.json` is present with an `apiKeyHelper`
- **THEN** `ensure-proxy-provider` still derives the provider from it

#### Scenario: Idempotent when already configured
- **WHEN** `~/.pi/agent/models.json` already defines a proxy provider
- **THEN** `ensure-proxy-provider` makes no changes
