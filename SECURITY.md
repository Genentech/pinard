# Security Policy

## Reporting a vulnerability

We take the security of Pinard seriously. If you believe you have found a security
vulnerability, please report it **privately** — do not open a public issue or pull
request for security problems.

To report a vulnerability, use GitHub's
**[Private vulnerability reporting](https://github.com/Genentech/pinard/security/advisories/new)**
— click **"Report a vulnerability"** under the repository's *Security* tab. This
opens a private security advisory visible only to the maintainers.

> **Note:** Private Vulnerability Reporting must be enabled on the repository
> (Settings → Code security and analysis).

Please include, where possible:

- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept if available)
- Affected version(s) / commit
- Any suggested remediation

## What to expect

- **Acknowledgement:** we aim to acknowledge your report within a few business days.
- **Assessment:** we will investigate, determine severity, and keep you informed
  of progress.
- **Disclosure:** we practice coordinated disclosure. Please give us reasonable
  time to release a fix before any public disclosure. We are happy to credit
  reporters who wish to be acknowledged.

## Supported versions

Pinard is under active development. Security fixes are applied to the latest
release / the default branch. Older versions are not guaranteed to receive
backported fixes.

## Handling of secrets

Pinard is configuration-driven and ships **no** credentials or private endpoints.
Never commit real secrets (tokens, credentials, private hostnames). A denylist
guard (`scripts/check-internal-refs.sh`) and CI checks help enforce this; report
any accidental secret exposure through the private channel above.
