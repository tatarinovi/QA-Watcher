# Security policy

## Supported versions

QA Watcher is currently maintained on the `main` branch. Security fixes are
provided for the latest revision only.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or credential
exposure. Use GitHub's **Report a vulnerability** form in the repository's
Security tab to submit a private security advisory.

If private vulnerability reporting is temporarily unavailable, open a minimal
issue asking the maintainers to establish a private contact channel. Do not put
vulnerability details in that issue.

Include the affected revision, impact, reproduction steps, and any suggested
mitigation. Do not include real access tokens, chat identifiers, private GitLab
URLs, or production data. Replace sensitive values with `[REDACTED]`.

## Security boundary

QA Watcher is designed for a trusted local host and a read-only GitLab token.
The MCP endpoint is loopback-only by default and has no remote authentication.
Do not expose it to a LAN or the public internet. See
[`docs/threat-model.md`](docs/threat-model.md) for the complete boundary and
known limitations.
