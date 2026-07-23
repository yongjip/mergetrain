import assert from "node:assert/strict";
import test from "node:test";

import {
  NEXT_ACTION_COPY,
  SSE_RECONNECT_GRACE_MS,
  actionCopy,
  newestFirstFifoRows,
  queuedAfterCurrentBatch,
  reconnectDelay,
  workspaceStepForSnapshot,
} from "../src/dashboardLogic.js";

const SERVER_NEXT_ACTIONS = [
  "upgrade_mergetrain",
  "unlock_wedged_runner",
  "wait_for_runner",
  "reconcile_pending_deploy",
  "reconcile_conflict_manual",
  "fix_blocked_job",
  "verify_reconciled_deploy",
  "deploy_validated_train_when_approved",
  "cancel_and_reenqueue_legacy_validated_jobs",
  "run_daemon_or_run_batch_deploy_when_approved",
  "run_batch_validate",
  "gc_available",
  "enqueue_clean_branch",
];

test("every server next_action has dashboard copy", () => {
  assert.deepEqual(Object.keys(NEXT_ACTION_COPY).sort(), SERVER_NEXT_ACTIONS.sort());
  assert.deepEqual(
    actionCopy("deploy_validated_train_when_approved", { action: "integrate", noun: "integration" }),
    ["Approve the exact validated train to integrate.", "Git integration remains an explicit CLI action."],
  );
});

test("planned SSE reconnect remains live for the grace window", () => {
  assert.equal(reconnectDelay(0, 1000), 0);
  assert.equal(reconnectDelay(1000, 1000), SSE_RECONNECT_GRACE_MS);
  assert.equal(reconnectDelay(1000, 7000), 1000);
  assert.equal(reconnectDelay(1000, 8000), 0);
});

test("FIFO rows display newest first without changing processing order", () => {
  const rows = newestFirstFifoRows([
    { id: 3 },
    { id: 1 },
    { id: 4 },
    { id: 2 },
  ]);
  assert.deepEqual(rows.map(({ job }) => job.id), [4, 3, 2, 1]);
  assert.deepEqual(rows.map(({ order }) => order), [4, 3, 2, 1]);
});

test("requests arriving after a batch starts wait for the next batch", () => {
  const jobs = [
    { id: 1, status: "in_progress" },
    { id: 2, status: "in_progress" },
    { id: 3, status: "queued" },
    { id: 4, status: "queued" },
  ];
  const running = queuedAfterCurrentBatch(
    { train: { selection: "running" }, jobs },
    jobs.slice(0, 2),
  );
  assert.deepEqual(running.map((job) => job.id), [3, 4]);
  assert.deepEqual(
    queuedAfterCurrentBatch(
      { train: { selection: "queued" }, jobs },
      jobs,
    ),
    [],
  );
});

test("live runner progress never renders as validated before gates finish", () => {
  assert.equal(workspaceStepForSnapshot({
    train: { selection: "running", jobs: [{ id: 50 }] },
    progress: { phase: "gating", completed_job_ids: [50] },
  }), 5);
  assert.equal(workspaceStepForSnapshot({
    train: { selection: "validated", jobs: [{ id: 34 }] },
    progress: { phase: "ready" },
  }), 6);
});
