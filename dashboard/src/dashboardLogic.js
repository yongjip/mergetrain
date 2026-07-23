export const NEXT_ACTION_COPY = {
  upgrade_mergetrain: ["Upgrade mergetrain before continuing.", "This repository uses a newer config contract than the installed CLI understands."],
  unlock_wedged_runner: ["Inspect and unlock the wedged runner.", "Its lease expired while work still appears active; confirm the old process cannot push before forcing an unlock."],
  reconcile_pending_deploy: ["Reconcile the interrupted deploy before any new run.", "Compare the write-ahead marker with the configured remote refs, then apply the recorded recovery decision."],
  reconcile_conflict_manual: ["Resolve the deploy reconciliation conflict manually.", "The remote refs and write-ahead marker disagree; inspect the recorded SHA and refs before changing queue state."],
  verify_reconciled_deploy: ["Verify the reconciled deploy.", "The remote landed, but post-push verification could not be proven automatically."],
  wait_for_runner: ["Wait for the current phase to finish.", "The runner will continue automatically."],
  fix_blocked_job: ["Fix the blocked branch and enqueue again.", "Commit a clean result in the owning branch first."],
  deploy_validated_train_when_approved: ["Approve the exact validated train to {action}.", "Git {noun} remains an explicit CLI action."],
  cancel_and_reenqueue_legacy_validated_jobs: ["Re-enqueue the legacy validated jobs.", "A fresh train identity is required before {noun}."],
  run_daemon_or_run_batch_deploy_when_approved: ["Start the approved {action} runner.", "Only auto-approved jobs are eligible for the daemon."],
  run_batch_validate: ["Start a validation run when ready.", "Nothing will be pushed in validate-only mode."],
  gc_available: ["Clean up completed worktrees.", "Review the dry run before applying cleanup."],
  enqueue_clean_branch: ["Enqueue a committed task branch.", "The queue is ready for the next clean job."],
};

export const REMEDIAL_ACTIONS = new Set([
  "upgrade_mergetrain",
  "unlock_wedged_runner",
  "reconcile_pending_deploy",
  "reconcile_conflict_manual",
  "verify_reconciled_deploy",
  "fix_blocked_job",
]);

export function actionCopy(value, words) {
  const template = NEXT_ACTION_COPY[value] || NEXT_ACTION_COPY.enqueue_clean_branch;
  return template.map((line) => line
    .replaceAll("{action}", words.action)
    .replaceAll("{noun}", words.noun));
}

export const SSE_RECONNECT_GRACE_MS = 7000;

export function reconnectDelay(lastLiveAt, now = Date.now()) {
  if (!lastLiveAt) return 0;
  return Math.max(0, SSE_RECONNECT_GRACE_MS - (now - lastLiveAt));
}

export function newestFirstFifoRows(jobs = []) {
  return [...jobs]
    .sort((a, b) => Number(a.id) - Number(b.id))
    .map((job, index) => ({ job, order: index + 1 }))
    .reverse();
}
