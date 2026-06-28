# Security notes

## Config trust boundary

`.trainyard.yaml` is trusted code. Gate and verify commands run through
`/bin/sh` in the integration worktree. Do not use untrusted config files.

## Secrets

- Do not store provider tokens or credentials in `.trainyard.yaml`.
- Prefer environment variables, your shell environment, or a service-specific
  secret manager.
- Logs may contain command output. Gate and verify commands should avoid printing
  secrets.

## Network access

`deploy.verify` hooks can run arbitrary network commands. Review verify hooks
before enabling unattended daemon deployment.

## Examples

Secret-scan examples are intentionally generic. They are not a replacement for a
real secret scanning policy.
