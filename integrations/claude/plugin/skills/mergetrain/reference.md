# mergetrain protocol reference

## `next_action` guide

| `next_action` | Response |
| --- | --- |
| `upgrade_mergetrain` | Upgrade before mutating state; the config is newer than this runtime. |
| `unlock_wedged_runner` | Report the expired live/unknown owner; require confirmation before `unlock --force`. |
| `wait_for_runner` | Observe with inspect, bounded events, or logs. Do not start a second runner. |
| `reconcile_pending_deploy` | Inspect `reconcile --json`; require confirmation before applying recovery. |
| `reconcile_conflict_manual` | Inspect the marker-bearing conflict and report the required Git repair. |
| `fix_blocked_job` | Fix and commit in the owning branch, then use `retry <id>`. |
| `verify_reconciled_deploy` | Re-run verification or record an explicit acknowledgement. |
| `deploy_when_approved` | Continue under bounded unattended approval; otherwise run `deploy` and keep identity details internal. |
| `cancel_and_reenqueue_legacy_validated_jobs` | Explain that legacy validation lacks a deployable identity; do not cancel without approval. |
| `run_daemon_when_approved` | Auto work exists; unattended deploy still requires explicit approval. |
| `validate_queued_jobs` | Run `validate`; it never pushes. |
| `reconcile_stranded_claim` | Inspect recovery state; require confirmation before applying `reconcile --apply`. |
| `initialize_config` | Run init in preview mode or ask before writing project files. |
| `gc_available` | Show the dry-run candidate list; deletion requires separate approval. |
| `enqueue_clean_branch` | Enqueue only a committed, clean task branch; exact SHA capture is automatic. |

## MCP refusal and adapter errors

| `error.code` | Meaning |
| --- | --- |
| `cli_timeout` | The bounded CLI child exceeded its deadline. |
| `cli_output_unreadable` | The CLI did not return a JSON object; do not guess the outcome. |
| `log_unavailable` | The requested raw log could not be read. |
| `invalid_inspect_detail` | Detail must be summary, events, or logs. |
| `confirmation_required` | The client cannot render the human confirmation dialog; use the returned ordinary deploy command. |
| `deploy_not_confirmed` | The human did not complete the deploy confirmation; nothing was pushed. |
| `deploy_plan_unavailable` | CLI preview did not provide the plan identity, so confirmation was not attempted. |

For CLI-originated errors passed through by the MCP server, branch on the
returned `error.code` and follow its `next_action` when present.
