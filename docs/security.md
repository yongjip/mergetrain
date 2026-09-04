# Security notes

## Runner and task-agent credentials

Mergetrain's SQLite lock and lease token guarantee that one current mergetrain
runner owns a train. They are not an operating-system or remote authorization
boundary. A coding agent with general shell access and a credential accepted by
the integration branch can run `git push` directly and bypass the queue.

Use capability separation when the one-runner property must be enforced:

- task agents may read and write their worktrees, commit, and enqueue exact
  SHAs, but receive no credential that can update the integration branch;
- the runner uses a separate SSH key or token allowed to update the configured
  integration branch and create `refs/mergetrain/deploys/*`;
- the remote protects `main` and the integration branch, allowing only the
  runner identity or a reviewed PR workflow; and
- start with an `agent-integration` branch and one reviewed PR to `main` when a
  repository is adopting the model for the first time.

MCP can narrow the actions available through that server, but it does not revoke
capabilities exposed by a separate general shell. Repository instructions are a
behavioral protocol, not a substitute for credential or branch protection.

In particular, `mergetrain_deploy` mechanically requires client-rendered human
acceptance on the MCP surface. The internal CLI continuation using an expected
plan remains non-interactive so the confirmed MCP request can finish. An agent
with arbitrary shell access under the same operating-system identity is
therefore governed by the host's shell permissions, credentials, and operating
instructions, not by the MCP confirmation mechanism. Mergetrain cannot create
an OS security boundary against another process running as the same user.

Give the runner the minimum remote permission needed for configured payload
refs and permanent audit refs. Keep provider-specific credential setup outside
core configuration and examples.

## Config trust boundary

`.mergetrain.yaml` is trusted code. Gate and verify commands run through a
resolved POSIX `sh` (`/bin/sh`, `sh` from `PATH`, or Git-for-Windows
`sh.exe`) in the integration worktree. Do not use untrusted config files.
Their environment prioritizes the directory containing the Python interpreter
that launched mergetrain, then preserves the inherited `PATH`. Run mergetrain
from a reviewed virtualenv or installation because executables beside that
interpreter can satisfy bare gate commands such as `ruff`, `mypy`, or `pytest`.

Git remote configuration is also trusted deploy input. Mergetrain resolves one
effective push URL before approval, rejects multiple or relative local
destinations, and pins that endpoint through audit, push, and recovery checks.
This prevents configuration drift from redirecting an approved mergetrain
operation; it does not stop an actor who already has shell and integration
credentials from invoking `git push` outside mergetrain.

Unattended approval also binds the effective gates, default command timeout,
validation-reuse policy and authorization, and verify hooks. The daemon checks
that identity during claim, and the runner reloads the trusted control-checkout
configuration before gates and before creating a push marker. Policy drift
therefore requires a fresh approved enqueue instead of silently weakening QA.

## Secrets

- Do not store provider tokens or credentials in `.mergetrain.yaml`.
- Prefer environment variables, your shell environment, or a service-specific
  secret manager.
- Logs may contain command output. Gate and verify commands should avoid printing
  secrets.

Structured surfaces apply one best-effort redaction policy to expected error
messages, persisted job notes, status JSON, diagnostic remote URLs, and dashboard
snapshots. MCP diagnostics synthesized when a CLI child does not return its JSON
contract use the same policy; valid CLI JSON remains contract-owned and is not
rewritten by the adapter. It masks sensitive `NAME=value` assignments,
sensitive command options such as `--token`, and passwords in URL userinfo
(`https://user:password@host`); the username is retained for diagnostics. This
is defense in depth, not a general secret scanner. Raw command logs can still
contain anything the subprocess printed, so the rules above remain mandatory.
Every structured copy of a persisted note applies redaction before its
1,000-character bound. Compact status publishes `reason_truncated`, serialized
jobs publish `note_truncated`, and note-derived outcome or progress messages
publish `message_truncated`, so a long note cannot expose a suffix or expand
CLI and MCP context without limit. This includes the no-event `inspect`
fallback. MCP continues to return valid CLI JSON unchanged; safety is enforced
at the shared projection source rather than relying on adapter rewriting.

## CLI observability boundaries

`events --jsonl` and `inspect --json` expose structured phases, bounded/redacted
command templates, status notes, SHAs, failure categories, and lease timing. They
do not copy gate, push, or verify stdout/stderr into event records. Error event
details expose a return code rather than subprocess output. Lease/claim tokens are
never serialized.

`stats --json` emits fixed status/reason categories and aggregate timing/counts.
It may use redacted-compatible job fields and legacy note text while classifying
an outcome, but it never emits task text, branch names, note text, or claim
tokens. Its batching estimate joins gates to runs internally and exposes only
aggregate counts, seconds, and the fixed estimation method.

`logs` is the explicit opt-in path to raw local command output and therefore may
show secrets that a command printed. It accepts only a job ID and refuses a stored
path outside configured `state.logs`. Protect that directory and do not forward
log output to an untrusted channel. JSONL event and heartbeat frames remain safe
to resume by event ID; heartbeat frames are ephemeral and contain no process owner
or lease token.

## Network access

`deploy.verify` hooks can run arbitrary network commands. Review verify hooks
before enabling unattended daemon deployment.

## Release trust boundary

Production publishing is authorized by two inputs that the release tag cannot
define for itself: the `release.yml` workflow running at `refs/heads/main`, and
the SSH allowed-signers policy from that main checkout. The workflow verifies
an annotated tag, requires its peeled commit in freshly fetched `origin/main`,
and passes the captured commit SHA to every source checkout. It also requires a
human-published immutable GitHub Release for the same tag before build or OIDC
publication.

The GitHub `pypi` environment must allow only branch `main`; an in-workflow ref
check is defense in depth, not a replacement for that repository setting.
Release signing protects against tag/release authority that cannot also modify
the trusted main policy. It does not claim to survive simultaneous compromise
of the repository administrator, signing key, and package-index account.

## Remote deploy audit refs

Every deploy atomically writes `refs/mergetrain/deploys/<sha>` alongside the
configured payload refs. The ref contains no credential or metadata beyond the
commit ID, but it is intentionally retained as recovery evidence. Grant the
runner permission to create that namespace and do not delete or rewrite it in
normal repository maintenance. Mergetrain checks the exact existing value and
uses `--force-with-lease`, so a mismatched or concurrently changed audit ref
rejects the whole atomic push instead of being overwritten.

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

Runtime provenance from `status --diagnose` is intentionally CLI-only because
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
