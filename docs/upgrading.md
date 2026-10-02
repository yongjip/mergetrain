# Upgrade mergetrain while preserving existing work

Use this procedure for a requested upgrade or a session that encounters mixed
mergetrain versions. Keep the requested target release. A version or contract
mismatch is a compatibility problem to investigate; it does not by itself
justify downgrading, reverting the project, or replacing the queue.

An upgrade request covers the installation and consumer changes within its
scope. It does not grant new deployment, unattended-runner, or recovery
authority. Preserve existing approval when its destination and execution policy
still match; a release number alone does not invalidate that approval.

## Record the target and every running version

Record the target release from the request or the repository's agreed pin,
then read the queue before changing it:

```sh
mergetrain --repo /absolute/path/to/project status --json
mergetrain --repo /absolute/path/to/project status --diagnose --json
mergetrain --version
```

Read `health`, `state`, and `next_action` together. Record the control checkout,
config path, queue path, destination, and effective gates/reuse/verify policy.
Use `inspect JOB_ID --json` when a job's evidence is needed.

Inventory the components separately:

| Component | Evidence to collect |
| --- | --- |
| CLI used by each wrapper or agent | Resolved executable, `--version`, and diagnostic `runtime` package path; locate all PATH matches, for example with `type -a mergetrain` on Bash/Zsh. |
| Existing daemon or Hub daemon | Process ID, launch command, start time, executable environment, and the service or wrapper that owns it. Check its logs and recorded launch configuration; a Hub runner covers multiple repository queues. |
| MCP and plugins | Installed plugin version, its package pin, and the running server's launch command. A global CLI update does not update this process. |
| Consumers | Version pins and assertions on `contract_version`, command exit codes, response fields, and safety actions in scripts and agent instructions. |

`status --diagnose` describes the CLI answering that request. It does **not**
report the version of an already-running daemon. An idle daemon may hold no
runner lease and still be alive. A PID or a wrapper's "already running" result
proves liveness, not that it loaded the target release. Installing a new wheel
or refreshing a plugin also does not replace code already loaded in a process.
A process can even retain loaded code after a package manager removed its old
installation; a later lazy import may then fail. Verify that the running
process's environment still exists, rather than inferring its version from the
current executable at the same path. Treat a handoff's commands and proposed
phases as a plan, not proof they have already run; reread live state.

Use the environment the gates are meant to run in. When they depend on a
project's development tools, invoke that environment's Python explicitly, for
example `/absolute/path/to/project/.venv/bin/python -m mergetrain`. Gate commands
prioritize sibling executables from that Python environment. A globally
installed CLI can select its own Python without `pytest` even when a project's
environment previously passed validation. Select the intended environment;
keep the approved gates and verification hooks.

## Adapt consumers using the release migration

Read every intervening release's Upgrade notes in the [changelog](../CHANGELOG.md)
and the matching migration in the [machine contract](contract.md). Identify the
commands and fields the repository actually consumes. Check exit semantics as
well as JSON keys. Continue compatible reads and authorized repairs while an
unsupported mutation remains stopped; a **Breaking** label is not a reason to
abandon the requested upgrade.

For 3.3.x to 3.4.x:

- JSON reports contract 5. The removed fields are `eta` and `progress` in the
  repository snapshots of full `hub status --json`. A consumer of `status` or
  `inspect` can accept contracts 4 and 5 after checking the migration and
  testing its existing field and safety checks.
- `daemon --once` now exits 1 for an error, a pending reconcile, or processed
  jobs that did not all land (or validate with `--validate-only`). Read its
  outcome and queue state rather than treating every nonzero exit as a crash.
- A linked worktree now reads the control checkout's configuration. Compare
  that source with the intended policy; do not restore the task worktree's
  configuration merely to reproduce the old behavior.
- Stricter YAML and URL checks may reject an existing configuration. Fix the
  named invalid value while preserving the intended gates and destination.

A reviewed status/inspect consumer can use an explicit compatibility set:

```python
SUPPORTED_CONTRACTS = {4, 5}  # Reviewed for the fields this consumer reads.
if payload.get("contract_version") not in SUPPORTED_CONTRACTS:
    raise ValueError("unsupported mergetrain contract")
```

Keep the remaining shape, `ok`, `health`, outcome, destination, and safety checks.
Preserve the reported contract version when normalizing a payload. Do not
accept all future versions, relabel contract 5 as 4, or remove a check just to
make a wrapper pass. Exercise the consumer's ordinary, Attention, and failure
responses, including refusal of unknown safety actions and unsupported
contracts. Use fixtures or disposable repositories for checks that would
otherwise validate or deploy live work.

## Keep the control checkout ready for the handover

In 3.4, default policy comes from the control checkout's working tree. Project
wrappers may also run from that checkout and require its configured integration
branch, such as `main`, and a clean working tree before an issue-backed landing.
Check the project's actual requirements before changing any installation or
runner. When those requirements apply, a task branch in the control checkout
leaves a handover precondition unmet. Preserve the task and restore the required
checkout before proceeding.

Have the owning agent commit its task and continue in a separate task worktree;
then return the control checkout to the required branch without discarding any
work. Do not reset a dirty checkout or move another agent's branch behind it.
Keep policy changes in the normal reviewed integration path. Once landed,
fast-forward the control checkout as permitted by the project's protocol and
confirm `config_drift` with `status --diagnose`. Switching branches while a
runner uses the control checkout can change the policy it reads.

## Land compatibility fixes before changing the runner

For a planned upgrade, land and check consumer support for both the current
and target contracts using the current authorized runner first. Then finish its
in-flight work, move the pin, and restart. This order avoids requiring the
consumer fix to pass through the very wrapper it repairs.

If the target is already installed and the wrapper blocks its own compatibility
fix, record the bootstrap problem explicitly:

- Identify the existing fix branch, exact reviewed commit, review/risk evidence,
  and owning clean worktree. Reuse that fix when it still applies instead of
  creating competing replacements.
- Check the control checkout and queue preconditions before changing anything.
  A mismatched branch, dirty checkout, or running job leaves the planned
  handover pending; it does not authorize resetting work or stopping a push.
- Use an already approved integration path that carries the project's required
  issue, risk, and exact-SHA evidence. A native CLI route or running the reviewed
  wrapper from its task worktree is an option only if the project permits it
  and all those checks remain enforced. Do not bypass review or push directly
  just because the usual wrapper is blocked.
- When that approved path genuinely requires an older CLI, agree on a temporary
  compatibility bridge with an explicit exit: land the reviewed consumer fix,
  confirm it is on the integration branch, restore the target pin, and restart
  the target runner. Prefer a separately pinned environment when the project
  permits it. Check queue/config compatibility, destination/policy bindings,
  and launch options before using it. Equal SQLite schema numbers alone do not
  prove every execution behavior is compatible.

Keep the target release in the plan throughout a temporary bridge. Do not
silently downgrade a machine-wide installation, leave it on the earlier pin,
replace the queue, or broaden runner authority. If an operator already approved
that exact bridge and its scope, carry it through its recorded phases without
asking again for each job or opaque train ID. Otherwise prepare the concrete
handover and obtain the missing operator direction before changing the runner.
An unexecuted bridge proposal is not a completed rollback.

After the fix lands and the target runner is verified, resume the waiting
sessions on their existing branches. A branch that still carries an old wrapper
must incorporate the landed fix first: have its owning agent bring the
integration branch forward under the project's merge or rebase policy, preserving
the task commits. Rerun the blocked completion wrapper against current state;
check for an existing exact-SHA job before enqueueing again. Leave unrelated
blocked jobs to their own `status.next_action` recovery.

## Replace the runner at a safe boundary

Coordinate submitters and the service's restart policy so one runner owns the
handover. Let in-flight ticks finish and confirm no managed repository has a
running job before changing the environment. For a Hub daemon, inspect every
registered repository and wait for the entire sweep to finish. Restart both
single-repository daemons and Hub daemons at this idle execution boundary;
queued and Ready work can remain. The native daemon handles a stop signal by
finishing its current tick; use the existing service or wrapper's graceful-stop
procedure and wait for the
process to exit. A service manager's forced-stop timeout is not evidence that
the tick finished. Do not force-kill a push to speed up an upgrade.

Reread status after the stop. Resolve stranded claims or ambiguous pushes only
through the stated recovery action and its existing authority. Keep queued jobs,
exact SHAs, Ready trains, the database, and permanent deploy audit refs. There is
no need to cancel pending work or re-enqueue it solely because the release changed.

Move the installation pin and update the service's launch configuration to the
chosen executable or versioned environment. Verify that exact executable's
`--version`; a higher-priority old PATH entry can otherwise win again. Update
wrapper overrides such as `MERGETRAIN_BIN` only where the wrapper supports them.
Preserve the repo/config/db arguments, runner mode, interval, notification
settings, and deployment environment. For a Hub daemon, preserve its registry,
repository opt-outs, and concurrency setting too. Do not replace `daemon --validate-only`
with a deploying daemon or broaden the approved destination or execution policy.

Restart only the previously authorized runner within its unchanged scope, then
verify the new process and launch path. If a lease, policy identity, or recovery
state no longer matches, follow that evidence instead of granting a new approval
or copying a policy hash onto an old job. Refresh the plugin and reload its MCP
server according to the [client's update procedure](install.md#updating-the-plugins).
The submitting CLI, restarted daemon, and loaded MCP package should agree on the
target release.

## Bring existing agents forward

Where a project uses generated sidecars, the target installation can refresh them
without rewriting `.mergetrain.yaml`:

```sh
mergetrain --repo /absolute/path/to/project init --refresh-instructions
```

Review and commit them, and link them from the project's standard `AGENTS.md` or
`CLAUDE.md` as described in the [agent contract](agent-contract.md). Commit consumer
and instruction changes on a clean task branch and use the project's normal
integration path. Do not deploy that branch merely to test this procedure.

An existing conversation can retain the previous rules after a tool refresh.
Give it the target, the changed consumer checks, and the verified runner path,
and ask it to reread the project's instructions. Existing branches and commits
can continue; this is not a reason to discard the task or recreate its queue.
For example:

> Continue the existing task with mergetrain target 3.4.1. Read the project's
> current agent instructions and the upgrade guide. Check the CLI, running
> daemon, and MCP versions separately; adapt affected consumers using the
> documented contract 4-to-5 migration. Preserve existing branches, queued SHAs,
> and approval scope. A contract assertion or stale daemon is not a reason to
> downgrade. Report the exact remaining incompatibility if it cannot be repaired
> within the authorized task.

Finish with the target release, CLI/daemon/MCP evidence, consumer checks, queue
health and next action, and any remaining blocker. Reading an empty healthy queue
confirms observation only; it does not prove that a new deployment ran.

## Distinguish compatibility repair from rollback

| Evidence | Next step |
| --- | --- |
| New CLI/MCP with an old live daemon or a removed old environment | Finish the old tick, then replace the authorized runner using the verified target path. |
| Control checkout violates the project's branch/cleanliness preconditions | Let the owning agent preserve its work and restore the required control checkout before the handover. |
| The consumer fix is blocked by its own completion wrapper | Follow the reviewed bootstrap path above; keep the target and explicit exit conditions. |
| Contract 5 rejected by a reviewed contract-4 status/inspect wrapper | Adapt and check the consumer while retaining its safety checks. |
| `approval_execution_policy_changed` or `approval_destination_changed` | Inspect the affected job and actual config/destination change; use the [authorization recovery procedure](failure-modes.md#deploy-authorization-changed). |
| Stranded claim or ambiguous push | Follow `status.next_action` and preserve recovery evidence. |
| Reproduced regression in the target release after these checks | Record a minimal reproduction, impact, and repair or rollback options for the operator. |

Use rollback when the operator requests it, including an already authorized
rollback plan. Do not silently move the pin backwards to satisfy a stale
consumer. A rollback also needs a compatible queue/config schema and must retain
push and verification evidence; never rewrite the schema or restore an older
database just to make the old binary run.
