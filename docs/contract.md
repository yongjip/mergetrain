# Machine contract and compatibility policy

mergetrain is designed for coding agents and scripts that read state instead of
guessing. The checked-in contract fingerprints fail CI whenever a JSON surface
changes without an explicit compatibility decision.

## Independent versions

| Version | Location | Governs |
| --- | --- | --- |
| Product | `mergetrain --version` and `status --diagnose` | packaged release |
| `contract_version` | every JSON payload and JSONL `stream_start` | machine output semantics |
| Config `version` | `.mergetrain.yaml` | committed configuration schema |

mergetrain 3.0.4 uses machine contract **4** and config schema **2**. They move
only when their own boundary changes; neither is tied to the SQLite schema.

## Contract 4 envelope

Every one-shot JSON response carries top-level `contract_version`. Nested job
or repository objects are not stamped; the outer response owns the version.

`ok` means only that the command produced a normal response. Execution outcome
is separate:

- `validate` and completed deploy execution use `result` values
  `success`, `warning`, `partial`, or `failed`;
- `deploy --json` returns `result: "confirmation_required"` and never pushes;
- `status` uses `health` and `state`, not `ok`, for repository condition;
- advanced commands document their own `result` values.

All failures use one shape:

```json
{
  "contract_version": 4,
  "ok": false,
  "error": {
    "code": "queue_error",
    "message": "human-readable detail",
    "retryable": false
  }
}
```

`next_action` may accompany the envelope. Branch on `error.code`, not message
text.

## Stable state projection

`status --json` is the one state entry point. Its stable top-level concepts are:

- `health`: `healthy`, `unconfigured`, or `degraded`;
- `state`: `idle`, `waiting`, `running`, `ready`, or `attention`;
- `summary`;
- `next_action`: `code`, nullable `command`, `requires_approval`, nullable
  `target_job_id`, and nullable stable `reason_code`; the code, target, and
  command are one decision and never refer to different jobs;
- additive `warnings`, whose entries carry a stable `code`, `severity`, and
  human-readable `summary` without changing `health` or authorizing mutation;
- `counts`: `waiting`, `running`, `ready`, `attention`, and `done`;
- compact `attention_jobs` and `recent_jobs`, whose `state` uses the same four
  active values plus `done`, whose terminal detail stays in `outcome`, and
  whose actionable rows carry the same stable `reason_code` vocabulary;
  human-readable `reason` is secret-redacted before a 1,000-character bound,
  with `reason_truncated` recording whether the bound was applied.

Unknown and failed post-push verification remain in Attention until explicitly
resolved; a later deployment does not supersede unresolved health evidence.
A missing configured Git remote or integration ref makes the repository
`degraded`; status will not recommend enqueue until the base can be resolved.
`resolve_failed_verification` points at one exact deployed job. The existing
non-pushing `verify --job` recovery path uses that job to identify its
deployment generation, runs the matching verification policy once, and updates
all members atomically. If the persisted policy is absent or differs from the
current policy, the command fails closed and directs the operator to explicit
`--ack succeeded/failed`. Legacy rows without deployment identity are never
grouped by a guessed train ID or commit SHA. Status does not create a queue
database when none exists.

Internal queue, push, verify, and recovery states remain available through
`inspect` and diagnostics. Consumers should not reconstruct a competing state
machine from those fields.

Advanced structured views that serialize a job redact and bound its persisted
`note` at 1,000 characters and publish `note_truncated`. Outcome and progress
messages derived from that note use the same rule and publish
`message_truncated`. The truncation keys are always boolean; consumers should
show the bounded text and may use the flag to explain that more text was
discarded.

`next_action.code` and other enum-like values may grow. Consumers must preserve
unknown values, show their accompanying summary/message, and avoid mutation if
they do not understand the required action.

## JSONL streams

`events --jsonl` emits a `stream_start` record carrying `contract_version` on
every connection, including resumed connections. `event`, `heartbeat`, and
`stream_end` records do not repeat it. Dispatch every record by `type`; persist
only event IDs as resume cursors.

## `error.code` vocabulary

| `error.code` | `retryable` | Meaning |
| --- | --- | --- |
| `ambiguous_push` | no | the remote may have accepted a push; reconcile before another deploy |
| `approval_destination_changed` | no | unattended approval no longer matches the exact destination |
| `approval_execution_policy_changed` | no | unattended approval no longer matches gates, timeout, reuse, fingerprints, or verify policy |
| `cancellation_requested` | no | the active train was asked to stop |
| `command_failed` | no | a gate, verify hook, or Git command failed |
| `config_error` | no | configuration is absent, invalid, or too new for a state-changing command |
| `deploy_plan_changed` | no | the confirmed exact plan changed before push |
| `duplicate_active_branch` | no | the branch already has active queued work |
| `interrupted` | no | the process received an interrupt; exit 130 |
| `lock_held` | yes | another live runner owns the queue lease |
| `lost_lease` | yes | this runner no longer owns its lease |
| `merge_blocked` | no | the branch cannot be merged into the assembled train |
| `mergetrain_error` | no | an expected failure has no more specific code |
| `push_rejected` | no | remote policy or permissions rejected the push |
| `queue_busy` | yes | SQLite could not complete a write before its timeout; reread state because a push may already have happened |
| `queue_error` | no | a queue, job, or runner precondition failed |
| `reconcile_pending_deploy` | no | deployment is blocked until pending remote truth is reconciled |
| `remote_unreachable` | yes | recovery cannot yet inspect the pinned remote endpoint |
| `removed_interface` | no | a v2 command or option was used; the message gives the v3 replacement |
| `validated_train_pending` | no | validation paused because one exact train already awaits deploy approval |

`retryable` is authoritative for that response. Do not derive it from the code.

MCP may additionally return adapter refusals such as
`confirmation_required`, `deploy_not_confirmed`, `cli_timeout`,
`cli_output_unreadable`, and `log_unavailable`. They do not occur on CLI
output.

## Long-lived v3 compatibility policy

Version 3 is intended to be the final product grammar. There is no planned v4.
The following promises apply indefinitely across 3.x releases:

1. The six public CLI verbs—`init`, `status`, `enqueue`, `validate`, `deploy`,
   and `inspect`—will not be removed, renamed, or repurposed.
2. Existing public options keep their meaning. A new optional flag cannot make
   an old invocation more permissive or introduce a push.
3. Contract-4 JSON evolves additively: existing keys, types, meanings, failure
   envelope, and exit semantics are preserved.
4. Consumers ignore unknown object keys and tolerate unknown enum values. An
   unknown safety or next-action value must fail closed for mutation.
5. The five MCP tool names and their required inputs remain stable:
   `mergetrain_status`, `mergetrain_inspect`, `mergetrain_enqueue`,
   `mergetrain_validate`, and `mergetrain_deploy`.
6. Config schema 2 remains readable throughout 3.x. New settings are optional
   and receive safe code defaults; unknown settings are never silently treated
   as authorization.
7. New capability must fit an existing verb or an advanced operator surface.
   Adding another public core command requires measured repeated need and a
   product-scope review.

An incompatible change is permitted only when continuing the old behavior
would itself violate a safety guarantee. Such a release must fail the unsafe
operation closed, document the migration, bump the affected machine contract,
and provide a direct diagnostic. Convenience or naming preference is not a
reason to break v3.

## Additive changes

The following do not change `contract_version`:

- a new optional key;
- a new enum value that old consumers can safely treat as unknown;
- a new JSONL frame type;
- more human-readable diagnostic text;
- a new advanced command that does not change existing invocations.

Removing or renaming a key, changing a type or meaning, changing whether an
invocation can push, changing exit semantics, or changing stream resume rules
is incompatible.

## Contract 3 to 4 safety migration

Contract 3 treated only the latest inferred deployment generation's known
verification failure as current Attention. That inference could hide a failure
after an unrelated deployment because older rows did not retain destination,
policy, or generation identity. Contract 4 removes that unsafe supersession:
every unresolved known failure stays visible until `verify` or explicit
`--ack` resolves it. Consumers must not infer resolution from a later deployed
row. The `reason_truncated`, `note_truncated`, and `message_truncated` keys are
additive; the contract bump is for the Attention meaning change, not those
keys.

## Too-new configuration

State-changing paths (`enqueue`, `validate`, `deploy`, and daemons) reject a
config version newer than the running binary understands. Read-only inspection
and recovery remain available so a rollback cannot lock an operator out of
remote-truth reconciliation. `status --diagnose` reports the mismatch and the
safe next action.

## Enforcement

`tests/test_contract_fingerprints.py` captures recursive key sets for all core
JSON payloads, advanced machine surfaces, the failure envelope, and every JSONL
frame. It also compares the implemented error-code set with the table above.

Coverage is currently 22 surfaces: `status_diagnose`, `status`, `enqueue`,
`validate`, `gc`, `reconcile`, `unlock`, `verify`, `dismiss`, `retry`, `cancel`,
`hub_status`, `hub_status_summary`, `gc_applied`, `hub_add`, `hub_remove`,
`init`, `deploy_preview`, `inspect`, `history`, `stats`, and
`failure_envelope`, plus `_jsonl_frames`.

Run results share one builder, so the validation fingerprint also protects the
executed deployment-result shape.

An additive shape change requires review, a changelog note, and deliberate
golden regeneration. A removal or rename fails CI and is rejected unless it
meets the safety-exception policy. Semantic stability that key fingerprints
cannot detect is covered by focused contract tests and review.

The v2-to-v3 grammar break is intentionally concentrated in 3.0: ambiguous command
aliases, manually copied SHA inputs, human train IDs, separate doctor state,
and duplicate preview/reuse switches were removed together. From 3.0 onward,
the product grammar remains fixed. Contract 4 is the narrow safety exception
above and adds no command, option, config field, or MCP tool.
