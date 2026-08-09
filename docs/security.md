# Security notes

## Config trust boundary

`.mergetrain.yaml` is trusted code. Gate and verify commands run through a
resolved POSIX `sh` (`/bin/sh`, `sh` from `PATH`, or Git-for-Windows
`sh.exe`) in the integration worktree. Do not use untrusted config files.
Their environment prioritizes the directory containing the Python interpreter
that launched mergetrain, then preserves the inherited `PATH`. Run mergetrain
from a reviewed virtualenv or installation because executables beside that
interpreter can satisfy bare gate commands such as `ruff`, `mypy`, or `pytest`.

## Secrets

- Do not store provider tokens or credentials in `.mergetrain.yaml`.
- Prefer environment variables, your shell environment, or a service-specific
  secret manager.
- Logs may contain command output. Gate and verify commands should avoid printing
  secrets.

Structured surfaces apply one best-effort redaction policy to expected error
messages, persisted job notes, status JSON, `doctor` remote URLs, and dashboard
snapshots. It masks sensitive `NAME=value` assignments, sensitive command
options such as `--token`, and passwords in URL userinfo
(`https://user:password@host`); the username is retained for diagnostics. This
is defense in depth, not a general secret scanner. Raw command logs can still
contain anything the subprocess printed, so the rules above remain mandatory.

## CLI observability boundaries

`events --jsonl` and `inspect --json` expose structured phases, bounded/redacted
command templates, status notes, SHAs, failure categories, and lease timing. They
do not copy gate, push, or verify stdout/stderr into event records. Error event
details expose a return code rather than subprocess output. Lease/claim tokens are
never serialized.

`logs` is the explicit opt-in path to raw local command output and therefore may
show secrets that a command printed. It accepts only a job ID and refuses a stored
path outside configured `state.logs`. Protect that directory and do not forward
log output to an untrusted channel. JSONL event and heartbeat frames remain safe
to resume by event ID; heartbeat frames are ephemeral and contain no process owner
or lease token.

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

## Path-aware gates fail closed

Top-level train gates may declare repository-relative `paths` patterns. The
runner discovers changed paths from the captured integration base to the exact
deploy SHA and skips a scoped gate only when that comparison succeeds and no
path matches.

If either revision is unavailable, `git diff` fails, or its machine-readable
output cannot be parsed, mergetrain runs every affected scoped gate. Rename and
copy records retain both the old and new path so moving a file out of a guarded
area cannot bypass its gate. Patterns are validated as normalized relative
POSIX paths; absolute paths, traversal segments, backslashes, and ambiguous
embedded `**` forms are refused.

## Parallel gate process isolation

An explicit parallel gate group starts multiple trusted shell commands in the
same integration worktree. Configure only checks that are independent and do
not mutate shared files. Every command gets its own process group and buffered
log stream. A failure, timeout, lease loss, or cancellation terminates the other
groups before the runner records terminal events; on Windows the runner uses
`taskkill /T /F` to include descendants.

`gate_parallelism.max_workers` and each gate's `workers` weight bound scheduling,
not the operating system. A command that launches its own pool must cap that pool
itself and declare a representative weight. Logs remain raw and potentially
sensitive; structured failure events include only a return code or exception
class, never subprocess output.

## Dashboard exposure

`mergetrain dashboard` binds to `127.0.0.1:8765` by default and has no action
endpoints. Its payload omits lease tokens, local worktree paths, log paths, and
the username portion of the runner owner. Status notes and Git branch names are
still visible to anyone who can reach the server. Active gate events also include
the configured command template; obvious token/password assignments and flags are
masked by the same policy described above, but command authors should never
embed credentials directly in gate configuration.

Runtime provenance from `version` and `doctor` is intentionally CLI-only because
it can include an imported package path, editable source path, and source-control
state. The dashboard snapshot and remotely bindable dashboard API do not include
that provenance object.

Browser notifications are opt-in and page-owned. Their lock-screen-visible text
is deliberately limited to the project name, job IDs, status, and aggregate
counts; task text, branch names, notes, paths, commands, and error details are
not copied into an alert. Snapshot-read failures likewise use generic stale-state
copy rather than the underlying error message. The enabled preference and short-lived duplicate
suppression keys live in origin-scoped browser storage. Clicking an alert only
focuses the page and, in Hub, selects the affected repo; it does not call a
mutation endpoint. Closing the page stops new browser alerts.

Binding to a non-loopback host requires `--allow-remote`. That flag is an
acknowledgement, not an authentication or encryption layer. Put a separately
reviewed authenticated reverse proxy in front of the dashboard if it must be
reachable beyond the local machine. Do not expose it directly to an untrusted
network.

## Examples

Secret-scan examples are intentionally generic. They are not a replacement for a
real secret scanning policy.
