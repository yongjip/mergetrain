# Agent contract

Generated agent instructions stay small on purpose. A task agent needs five
rules:

1. Work on the assigned task branch and worktree.
2. Commit a clean HEAD before handoff.
3. Read `mergetrain status --json` and follow its stated next action.
4. Enqueue with task, branch, and optional worktree only; mergetrain captures
   exact commits.
5. Stop after enqueue unless the user explicitly authorized end-to-end
   validation and deployment. Never push integration refs directly.

Create or refresh the generated sidecars with:

```sh
mergetrain init --refresh-instructions
```

## Authority boundaries

A normal task request authorizes editing, testing, committing, and enqueueing.
It does not authorize deployment. A bounded instruction to QA, deploy, verify,
and finish the named task does authorize that end-to-end path without repeated
opaque train-ID questions.

`mergetrain deploy` shows a human-readable exact plan and requires attributable
confirmation. Unattended jobs use hidden `enqueue --auto` only after explicit
bounded approval; their destination and execution-policy hashes are rechecked
at claim, before gates, and before push.

Recovery and destructive cleanup are separate authority boundaries. Follow the
exact command returned by `status.next_action`; do not guess, directly edit the
queue database, or rewrite `refs/mergetrain/deploys/*`.

## State-guided detail

Routine instructions intentionally omit retry, supersede, reuse, daemon, Hub,
and recovery procedures. When one becomes relevant, `status` returns its exact
command and approval class. Use `inspect JOB_ID --json` for evidence about one
job.

The default daemon deploys only jobs explicitly enqueued with `--auto`.
`daemon --validate-only` handles manual jobs but never pushes. A destination,
gate, timeout, reuse-policy, fingerprint, or verify-hook change invalidates an
unattended approval.

After an ambiguous push, `reconcile --apply` resolves state against the exact
pinned remote endpoint and never repeats a push merely because the local row is
uncertain. Deployment remains blocked while any job needs reconciliation.

## Machine consumers

All JSON payloads carry `contract_version`. Branch on `error.code`, read
`next_action.command`, ignore unknown keys and enum values, and dispatch JSONL
records by `type`. The complete compatibility promise is in
[contract.md](contract.md).
