# The machine-readable contract

mergetrain is built so a coding agent can **read JSON and act instead of
guessing**. That only works if the JSON shape is stable and versioned. This
page is the contract: what every machine-readable surface guarantees, how it is
versioned, and what counts as a breaking change.

It is enforced, not aspirational — a checked-in golden fingerprint
(`tests/contract_fingerprints.json`) fails CI if any surface's shape changes
without a deliberate version decision.

## Three version numbers, kept separate

| Number | Where | Governs |
|---|---|---|
| `contract_version` | top of every `--json` payload / HTTP snapshot; the `stream_start` JSONL frame | the shape of machine-readable output |
| config `version:` | `.mergetrain.yaml` | the schema of the config file |
| `__version__` | `version` / `doctor` payloads | the product release |

`contract_version` (currently **2**) and the config `version` (currently **2**)
are deliberately distinct from the product `__version__`. A patch release that
changes no output shape never reads as a contract change, and vice versa. Each
is a single forward-only integer, matching the SQLite `SCHEMA_VERSION` and the
hub `REGISTRY_VERSION`.

## Where the number lives

- **One-shot `--json`** — a top-level `contract_version` on the outer object
  (`doctor`, `status`, `version`, `inspect`, `enqueue`, `run-batch`, `gc`,
  `reconcile`/`recover`/`unlock`, `hub status`, `agent-contract`, …). Nested
  sub-objects (a job dict, an embedded per-repo snapshot) are **not** stamped —
  the outer frame owns the number.
- **HTTP `/api/snapshot`** — stamped at the boundary; a hub payload carries one
  top-level number and its embedded per-repo snapshots stay bare.
- **`events --jsonl`** — a `stream_start` frame carrying `contract_version` is
  emitted on every connect (including an `--after` resume, which may be a
  different binary). The `event`, `heartbeat`, and `stream_end` frames do not
  carry it. **Dispatch JSONL on `type`.**

## The envelope (contract 2)

- **`ok` means exactly one thing:** the command executed without raising an
  error envelope. It is *not* a health verdict and *not* an outcome grade.
  - For `run-next` and `run-batch`, read `result`
    (`success`/`warning`/`partial`/`failed`) for the run outcome — a completed
    deploy with a post-push verify warning is `ok:true, result:"warning"`.
  - Other `result` fields are command-specific legacy surfaces:
    `verify` uses `success`/`failed`, `reconcile` and `recover` use
    `success`/`conflict`, and `gc` carries `null` for a dry run or the applied
    cleanup detail object. Consumers must dispatch by command before reading
    these values; normalizing them would require a contract-version bump.
  - Read `health` (on `doctor`) for the repo-configured-and-git-present verdict.
  - Read `removed` (on `hub remove`) for found-or-not.
- **`next_action`** is present on both `doctor` and `status` — the two reads an
  agent is told to take before acting — plus the recovery commands.
- **One failure shape everywhere:** `{ok:false, error:{code,message,retryable},
  next_action?}`. Branch on `error.code`. (The deploy-while-reconcile block is
  `error.code:"reconcile_pending_deploy"` with a top-level `next_action` and
  `needs_reconcile` count.)
  JSONL failures carry the same `ok:false` and `error` object inside a terminal
  `stream_end` frame, so a stream that emitted `stream_start` always terminates
  with a machine-readable record.

## `error.code` vocabulary

Branching on `error.code` only works if the codes are enumerated, so here they
are. Most are the snake-cased name of the raising error class; treat any code not
listed here as an unexpected failure and fall back to `message`.

| `error.code` | `retryable` | Means |
| --- | --- | --- |
| `config_error` | no | `.mergetrain.yaml` is missing, unparseable, invalid, or declares a `version` this build does not support |
| `queue_error` | no | a queue or lock precondition failed (nothing to run, bad job id, terminal job) |
| `duplicate_active_branch` | no | that branch already has a non-terminal job queued |
| `lock_held` | **yes** | another runner owns the queue lock; retry after it finishes |
| `queue_busy` | **yes** | a queue write did not happen: the database refused the writer because another process held it past `busy_timeout`. It does **not** mean nothing was pushed — the refs may already be on the remote, and the row is left as the last durable write left it for recovery to resolve. Retry, then read `status --json` |
| `lost_lease` | **yes** | this runner no longer owns the lease it was given; re-read state and retry |
| `merge_blocked` | no | the branch cannot be merged into the integration train |
| `command_failed` | no | a gate, verify hook, or git subprocess exited non-zero |
| `push_rejected` | no | the remote refused the push on policy or permissions; the job parks `blocked` |
| `ambiguous_push` | no | the push failed for a non-rejection reason, so the remote may have accepted it; the job parks `needs_reconcile` |
| `remote_unreachable` | **yes** | `reconcile` could not reach the remote to establish deploy truth; retry when connectivity returns |
| `cancellation_requested` | no | the active train was asked to stop |
| `reconcile_pending_deploy` | no | a deploy was refused because jobs are pending reconcile; carries `next_action` and a `needs_reconcile` count |
| `validated_train_pending` | no | `run-next` with a push mode was refused because a validated train is pending; carries `next_action` and `pending_train_ids`. Use `run-batch --deploy --train-id <id>` |
| `interrupted` | no | `Ctrl-C`; exit code `130` |
| `mergetrain_error` | no | an expected failure with no more specific class |

Retryable means only that the same call may succeed later without operator
intervention. Everything else needs a decision — read `message` and, when
present, `next_action`.

`retryable` comes from two rules, which is worth knowing because they can
disagree for the same class: the generic handler marks `LockHeld`, `LostLease`
and `QueueBusy` retryable, while the recovery commands (`reconcile`, `recover`,
`unlock`) mark their exit-code 3 and 7 failures retryable — which is why
`lock_held` and `remote_unreachable` are retryable there. A consumer should read
the flag rather than infer it from the code.

The MCP server adds refusal codes of its own for the surface it guards
(`confirmation_required`, `deploy_not_confirmed`, `train_id_required`,
`train_not_found`, `no_validated_train`, `cli_timeout`,
`cli_output_unreadable`, `log_unavailable`); see [mcp.md](mcp.md). They never
appear on CLI output.

## Compatibility policy

**Additive changes do not bump `contract_version`.** Consumers **must ignore
unknown keys** and **must dispatch JSONL on `type`**. Additive means: a new key
on a payload, a new optional field, a new JSONL frame type, a new command, a new
`next_action` value.

The optional `paths` array on top-level gate configuration and the `skipped`
gate event state are additive contract changes. Consumers must continue to
ignore unknown configuration keys and event state values. A skipped gate
includes the stable detail `no changed paths matched configured paths`.

The optional `parallel_group`, `needs`, `workers`, and `timeout_seconds` fields
on top-level gates plus the top-level `gate_parallelism` object are additive.
Parallel terminal gate events may use `failure` or `canceled` states; their
declaration-order indexes remain stable even when commands finish in a different
order.

Public job objects may include `supersession_id` and `supersedes_train_id`.
They link an atomically retired validated train to freshly queued replacement
jobs; they do not imply that validation or deploy approval transferred.

Dashboard snapshots and deploy-preview JSON may include the structured `reuse`
explanation with `identity_checks`, per-gate actions, and
`estimated_savings`. `eligible` is `null` when the dashboard has not performed
an exact preview, and `estimated_savings.authorizes_reuse` is always false.

`status` may include additive `counts`, `attention_jobs`, `jobs_limit`, and
`jobs_truncated` fields. `jobs` remains the newest limited history, while
`attention_jobs` is the uncapped action-required view. `hub status --summary`
is a separately fingerprinted compact view whose repo entries carry `summary`
instead of a full dashboard `snapshot`.

`stats` may include additive `current.window`, `current.gates`, and
`current.latency` objects. Established top-level aggregates retain their
selected-history meaning; automatic recommendations use the latest 20 complete
claim-token runs disclosed by `current.window`.

**Breaking changes bump `contract_version`** (a deliberate, reviewed decision):
removing or renaming a key, changing a value's type or meaning, changing the
`ok`/`result` semantics, changing exit codes, or changing the JSONL frame or
resume model.

For the config file: adding an optional key does not bump the config `version`
(unknown keys are tolerated). Bump only when an existing key's meaning changes
or a key becomes required — i.e. when an older binary would misread a newer
file. Both versions are forward-only; there is no down-migration.

Contract 2 removes presentation-only `human_vocabulary` and `terminology`
objects, the no-op `agent` config object, deploy spelling aliases, and the
redundant `hub list` surface. Version-1 config files migrate in memory; version-2
files reject the removed top-level keys.

### How a too-new config is handled

If a `.mergetrain.yaml` declares a `version:` newer than the running binary
understands, the **state-shipping path fails closed**: `enqueue`, `run-batch`,
and `run-next` refuse with a `config_error` envelope. **Recovery and read-only
commands stay permissive** — `reconcile`, `recover`, `unlock`, `status`,
`doctor`, `inspect`, `gc` (dry-run) all still run, so a rollback can never lock
you out of crash recovery. `doctor` reports `next_action: upgrade_mergetrain`
and `config_version_supported`.

## Enforcement

`tests/test_contract_fingerprints.py` captures a recursive key-set of each
surface and compares it to the checked-in `tests/contract_fingerprints.json`.
Any shape change fails CI and is classified — added keys are additive
(regenerate the golden with `MERGETRAIN_REGEN_FINGERPRINTS=1` and note it in the
changelog); removed or renamed keys are breaking and require bumping
`contract_version` first. It cannot detect a same-keys value-meaning change;
that residual rests on review.

Coverage is **every payload a command can emit**, currently 25 surfaces plus the
JSONL frames: `doctor`, `status`, `version`, `agent_contract`, `init`, `enqueue`,
`retry`, `inspect`, `history`, `stats`, `gc`, `run_batch_validate`,
`run_batch_preview`, `reconcile`, `recover`, `unlock`, `verify`, `dismiss`,
`cancel`, `gc_applied`, `hub_status`, `hub_status_summary`, `hub_add`, `hub_remove`,
`failure_envelope`,
and `_jsonl_frames`. A new `--json` payload must be added to `SURFACES` in the
same change that introduces it, or the gate silently does not cover it. Run
results share one builder, so `run_batch_validate` also pins `run-batch --deploy`
and `run-next`.

## Freeze linkage (0.9.0)

The 0.9.0 release candidate promises **additive-only changes within a contract
major**. That promise is mechanical, not a pledge: the fingerprint gate is what
makes it impossible to change a shape without a conscious version decision.
