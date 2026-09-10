# sandboxed-worker-bootstrap Specification

## Purpose
TBD - created by archiving change sandboxed-worker-bootstrap. Update Purpose after archive.
## Requirements
### Requirement: Sandboxed workers bootstrap secrets from revocable URLs
A `--containall` sandboxed worker SHALL obtain its runtime secrets at startup from
two environment-provided URLs rather than requiring host credential files to be
bind-mounted. `PINARD_UNCORK_URL` SHALL return a JSON manifest of files to
materialize (shared credentials + non-secret config). `PINARD_POUR_URL` SHALL
mint the per-operator LLM token used as the proxy credential. Both URLs are opaque
HTTP endpoints; the image itself SHALL contain no secrets.

#### Scenario: Uncork materializes the shared bundle
- **WHEN** a worker starts with `PINARD_UNCORK_URL` set to a valid endpoint
- **THEN** the bootstrap writes each file from the returned manifest under `$HOME`
  before the worker process starts
- **AND** the worker uses those credentials (e.g. `credentials.yaml`) to connect
  to NATS/engram/GitLab

#### Scenario: Pour supplies a per-operator token
- **WHEN** two operators start the same image and the same `PINARD_UNCORK_URL`
  but different `PINARD_POUR_URL`
- **THEN** each worker mints and uses its own LLM token
- **AND** revoking one operator's URL disables only that operator

#### Scenario: Bootstrap fails fast on a revoked or bad URL
- **WHEN** `PINARD_UNCORK_URL` returns a non-2xx status (e.g. `410 Gone`) or a
  malformed manifest
- **THEN** bootstrap aborts with a non-zero exit code and a clear message
- **AND** the worker process does not start

#### Scenario: Legacy fallback when no bootstrap URL is set
- **WHEN** `PINARD_UNCORK_URL` is unset
- **THEN** the worker proceeds using bind-mounted credentials (pre-existing
  behavior)

### Requirement: Bootstrapped secrets are never persisted to host disk
Secrets fetched at startup SHALL exist only in the container's ephemeral,
RAM-backed filesystem. Sandboxed workers SHALL run with
`--containall --writable-tmpfs` (or an equivalent tmpfs for the secret paths),
and materialized files SHALL be written mode `0600` into that ephemeral home.
Persistent artifacts SHALL be written only to explicitly bound persistent paths.

#### Scenario: Secrets vanish on stop
- **WHEN** the container stops
- **THEN** no bootstrapped credential material remains on the host or shared
  filesystem

### Requirement: The proxy provider is seeded from baked defaults and the pour URL
Seeding the pi proxy provider SHALL NOT require `~/.claude/settings.json`. The
provider registry (`~/.pi/agent/models.json`) SHALL be built from image-baked
non-secret defaults (base URL, headers, model ids), and the provider credential
(`~/.pi/agent/auth.json`) SHALL be minted from `PINARD_POUR_URL`. If
`~/.claude/settings.json` is present it MAY still be used (backward compatible).

#### Scenario: Models available without settings.json
- **WHEN** a `--containall` worker starts with no `~/.claude/settings.json` and a
  valid `PINARD_POUR_URL`
- **THEN** pinard resolves the `proxy/<model>` provider and obtains a token
- **AND** no "No models available" / "No API key" error occurs

### Requirement: Missing optional bind sources do not abort startup
The singularity launcher SHALL skip any `--bind` entry whose host source path does
not exist, emitting a warning, instead of allowing singularity to FATAL on a
missing bind source.

#### Scenario: A node lacks an optional bind path
- **WHEN** a configured bind source (e.g. a cert directory) is absent on the node
- **THEN** the launcher omits that bind with a warning and the worker still starts

