## ADDED Requirements

### Requirement: Sandboxed worker launch
The singularity launcher SHALL run sandboxed workers with
`singularity run --containall --writable-tmpfs <binds> <sif>` and SHALL pass the
bootstrap environment (`PINARD_UNCORK_URL`, `PINARD_POUR_URL`) through to the
container when set. `--writable-tmpfs` ensures the rootfs overlay (where
bootstrapped secrets are written) is RAM-backed and ephemeral.

#### Scenario: Launch a sandboxed worker with bootstrap env
- **WHEN** the launcher spawns a `runtime: singularity` worker with
  `PINARD_UNCORK_URL` / `PINARD_POUR_URL` in the environment
- **THEN** the `singularity run` invocation includes `--containall --writable-tmpfs`
- **AND** the bootstrap env vars are available inside the container

#### Scenario: Launch without bootstrap env
- **WHEN** the launcher spawns a sandboxed worker with no bootstrap URLs set
- **THEN** the worker still launches and relies on bind-mounted credentials
