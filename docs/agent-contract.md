# Agent contract

Agents interacting with mergetrain must follow this contract.

## Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly. For ordinary handoff, run
   `mergetrain enqueue` with the task, branch, and optional worktree only. Do
   not manually copy `--base-sha`, `--head-sha`, or `--capture-sha`; mergetrain
   captures and validates the exact commits by default. Stop after the
   successful enqueue unless separately authorized as the runner.
4. Read `mergetrain doctor --json` first. Use `mergetrain status --json --limit
   10` only when job or train details are needed, and read `attention_jobs`
   before recent history.
5. Use `--auto` only after explicit unattended-deploy approval. A bounded
   instruction to QA, deploy, verify, and finish end-to-end grants that approval
   for the named task scope, destination, and current trusted execution policy.
6. Reuse validated gates only after explicit config or `--reuse-validated`
   authorization.
7. Let one separately authorized runner or daemon own merge, test, push, and
   verify. Ordinary task, merge, integration, or enqueue intent is not deploy
   approval unless the user explicitly authorizes bounded end-to-end deployment.
8. Fix blocked or failed work in the owning branch, commit a clean result, then
   run `mergetrain retry <job-id>` to enqueue a fresh SHA-pinned job.
9. Replace a validated train only with `mergetrain supersede`; validate and
   approve the replacement as a new SHA-pinned train.
10. Do not delete or rewrite remote `refs/mergetrain/deploys/*`; they are
    permanent recovery evidence.

## Machine-readable contract

```sh
mergetrain agent-contract --json
```

The JSON payload includes `name`, `purpose`, `rules`, and `boundary`. Contract 2
uses one canonical `deploy` vocabulary and keeps the stable
`deployed`/`deploy_sha` machine state. Deploy completion names the atomic Git ref
update; it does not authorize or prove a downstream provider release.

## Next-action guidance

`mergetrain doctor --json` returns `next_action` values:

- `unlock_wedged_runner`
- `wait_for_runner`
- `reconcile_pending_deploy`
- `reconcile_conflict_manual`
- `fix_blocked_job`
- `verify_reconciled_deploy`
- `deploy_validated_train_when_approved`
- `cancel_and_reenqueue_legacy_validated_jobs`
- `run_daemon_or_run_batch_deploy_when_approved`
- `run_batch_validate`
- `recover_stranded_claim`
- `initialize_config`
- `gc_available`
- `enqueue_clean_branch`

`next_action` is advisory. It does not replace user approval for deploy,
unattended auto deploy, or destructive cleanup.

For a task agent, a successful exact-SHA enqueue is the terminal handoff. The
ordinary command omits SHA options; explicit SHA arguments are compatibility
inputs and a ready-checked enqueue rejects them when they do not exactly match
the current integration ref and clean task branch.
`run_batch_validate` and deploy-oriented next actions are runner/operator
guidance, not permission to continue after enqueueing. The same agent may act as
the runner when that role is separately authorized. Bounded end-to-end
authorization lets that runner continue through QA, deploy, and verification
without repeated train-ID prompts. The auto approval remains valid only while
the destination, gates, command timeout, validation-reuse policy, and verify
hooks match their enqueue-time identities.

The default daemon deploys only jobs explicitly enqueued with `--auto`.
`daemon --validate-only` is a separate runner authorization: it processes only
manual queued jobs, never pushes, and pauses while any validated train is
pending. Starting it authorizes merge and gates, not deploy.

After a crash or ambiguous push response, `reconcile`/`recover` resolve
`needs_reconcile` jobs against the remote (never re-pushing a landed deploy);
`run-batch --deploy` is refused while any job is `needs_reconcile`. See the
[failure modes guide](failure-modes.md).

When `validated_trains` is non-empty, mergetrain binds deployment to the exact
train identity and member HEADs internally. A later deploy must not silently
include newer queued jobs. For one-shot approval, summarize task intent and
changes, destination refs, gates, blocked or failed work, and reassembly risk;
the user can answer with a simple "deploy / yes / go" and never needs to repeat
an opaque train ID. Validated-but-not-deployed branches are not GC deletion
candidates. Deploy approval does not authorize gate reuse; that remains a
separate explicit policy decision.

Changing a train invalidates one-shot approval. `supersede` records the old/new
relationship atomically for audit, but the replacement never inherits
validation or gate-reuse identity. Bounded unattended authorization may
continue only while the named task scope and destination remain unchanged.
Scope or destination changes, product/business decisions, and destructive or
reconcile recovery boundaries require new authority.

When a runner is active, observe it with read-only commands instead of inspecting
the process tree:

```sh
mergetrain inspect <job-id> --json
mergetrain events --job <job-id> --after <last-event-id> --follow --jsonl
mergetrain logs <job-id> --follow --tail 20
```

Only persisted `type=event` IDs are resume cursors. `type=heartbeat` is ephemeral,
and `type=stream_end` states why a scoped follower stopped. Treat `logs` as raw
command output that may be sensitive; structured events do not include that
output.
