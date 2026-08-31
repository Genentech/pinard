# Contributing to Pinard

Thanks for your interest in contributing to Pinard! This document explains how to
build, test, and submit changes.

> **Note:** Pinard is developed on an internal source-of-truth and mirrored to
> the public repository at `github.com/Genentech/pinard`. External contributions
> are welcome via pull requests; maintainers may land them through the internal
> pipeline.

## Code of Conduct

By participating, you agree to uphold a respectful, harassment-free environment.
Be kind, assume good intent, and keep discussion technical and constructive.

## Getting started

### Prerequisites

- Go (see the version pinned in [`go.mod`](go.mod))
- Node.js ≥ 22.19.0 (for the `pi-extension` TypeScript components)
- `git` with submodule support (the `deps/babysitter` submodule)

```bash
git clone --recurse-submodules https://github.com/Genentech/pinard.git
cd pinard
```

### Configuration

Pinard is fully configuration-driven — no internal hosts or credentials are baked
into the code. Copy the example configs and fill in your own values:

```bash
cp credentials.example.yaml credentials.yaml
cp vignes.example.yaml vignes.yaml
```

See the `docs/` directory for the configuration reference.

## Build & test

```bash
# Build everything
go build ./...

# Run the Go test suite
go test ./...

# TypeScript extension checks (from pi-extension/)
npm ci && npm test
```

Optional / deployment-specific features are gated behind build tags and are
**off by default** in the public build (for example, the internal funding
integration builds only with `-tags capsule`).

## Guardrails

CI runs a secret scan (gitleaks) on every PR. Never commit credentials, tokens,
or private hostnames; PRs that introduce a detected secret will fail.

## Submitting changes

1. Fork and create a topic branch (`feat/…`, `fix/…`, `docs/…`, `chore/…`).
2. Keep changes focused; add tests for new behavior.
3. Ensure `go build ./...` and `go test ./...` pass locally.
4. Write clear commit messages (Conventional Commits style is appreciated:
   `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).
5. Open a pull request describing the change and its motivation.

### Developer Certificate of Origin (DCO)

Contributions must be made under the project's license. We use the
[Developer Certificate of Origin](https://developercertificate.org/): sign off
your commits with `git commit -s`, which adds a `Signed-off-by` trailer
certifying you have the right to submit the work under the MIT License.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
