# Security notes

## Config trust boundary

`.mergetrain.yaml` is trusted code. Gate and verify commands run through
`/bin/sh` in the integration worktree. Do not use untrusted config files.

## Secrets

- Do not store provider tokens or credentials in `.mergetrain.yaml`.
- Prefer environment variables, your shell environment, or a service-specific
  secret manager.
- Logs may contain command output. Gate and verify commands should avoid printing
  secrets.

## Network access

`deploy.verify` hooks can run arbitrary network commands. Review verify hooks
before enabling unattended daemon deployment.

## Validated-gate reuse fingerprints

Gate command/config text is not a complete environment fingerprint. The same
command can produce different results after an SDK update, compiler replacement,
container image change, runner OS update, or external dependency movement.
Environment-sensitive gates should configure `deploy.reuse.fingerprints` with
adapter-owned commands that emit stable opaque identities for every required
toolchain input, or be marked `always_rerun_on_deploy`. If a required identity
cannot be represented reliably, leave reuse disabled.

Fingerprint output is hashed before persistence and should never contain a
credential. The command itself still runs as trusted `/bin/sh` code. A changed,
missing, failed, multiline, or oversized fingerprint prevents reuse and follows
the configured rerun/fail-closed policy. Fingerprint commands should be
deterministic and side-effect-free because reuse preview executes them too.

## Dashboard exposure

`mergetrain dashboard` binds to `127.0.0.1:8765` by default and has no action
endpoints. Its payload omits lease tokens, local worktree paths, log paths, and
the username portion of the runner owner. Status notes and Git branch names are
still visible to anyone who can reach the server. Active gate events also include
the configured command template; obvious token/password assignments and flags are
masked, but command authors should never embed credentials directly in gate
configuration.

Runtime provenance from `version` and `doctor` is intentionally CLI-only because
it can include an imported package path, editable source path, and source-control
state. The dashboard snapshot and remotely bindable dashboard API do not include
that provenance object.

Binding to a non-loopback host requires `--allow-remote`. That flag is an
acknowledgement, not an authentication or encryption layer. Put a separately
reviewed authenticated reverse proxy in front of the dashboard if it must be
reachable beyond the local machine. Do not expose it directly to an untrusted
network.

## Examples

Secret-scan examples are intentionally generic. They are not a replacement for a
real secret scanning policy.
