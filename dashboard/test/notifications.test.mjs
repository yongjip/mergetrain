import assert from "node:assert/strict";
import test from "node:test";

import {
  claimNotification,
  feedErrorCandidate,
  notificationCandidates,
  readNotificationPreference,
  writeNotificationPreference,
} from "../src/notifications.js";

function snapshot({ selection = "running", jobs = [], trainId = "train-1" } = {}) {
  const trainJobs = jobs.filter((job) => ["in_progress", "validated"].includes(job.status));
  return {
    ok: true,
    project: { name: "api" },
    train: { selection, jobs: trainJobs },
    jobs,
    validated_trains: selection === "validated"
      ? [{ train_id: trainId, deploy_eligible: true, job_ids: trainJobs.map((job) => job.id) }]
      : [],
  };
}

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

test("the first dashboard snapshot is a quiet baseline", () => {
  const current = snapshot({ jobs: [{ id: 1, status: "failed" }] });
  assert.deepEqual(notificationCandidates(null, current), []);
});

test("feed failures use a stable generic attention notification", () => {
  const current = {
    ...snapshot({ jobs: [{ id: 1, status: "in_progress" }] }),
    generated_at: "2026-08-09T06:00:00Z",
  };
  const candidate = feedErrorCandidate(current, {
    code: "snapshot_unavailable",
    message: "token=secret-value at /private/worktree",
  });
  assert.equal(candidate.kind, "attention");
  assert.match(candidate.body, /last known state may be stale/);
  assert.doesNotMatch(JSON.stringify(candidate), /secret-value|private/);
  assert.deepEqual(
    feedErrorCandidate(
      { ...current, generated_at: "2026-08-09T06:00:05Z" },
      { code: "snapshot_unavailable", message: "different" },
    ),
    candidate,
  );
});

test("validation and deployment transitions produce one train notification", () => {
  const running = snapshot({
    jobs: [
      { id: 1, status: "in_progress", train_id: "train-1" },
      { id: 2, status: "in_progress", train_id: "train-1" },
    ],
  });
  const validated = snapshot({
    selection: "validated",
    jobs: [
      { id: 1, status: "validated", train_id: "train-1" },
      { id: 2, status: "validated", train_id: "train-1" },
    ],
  });
  const validationAlerts = notificationCandidates(running, validated);
  assert.equal(validationAlerts.length, 1);
  assert.equal(validationAlerts[0].kind, "validated");
  assert.match(validationAlerts[0].body, /awaiting deploy approval \(2 jobs\)/);

  const deployed = snapshot({
    selection: "idle",
    jobs: [
      { id: 1, status: "deployed", train_id: "train-1", verify_status: "succeeded" },
      { id: 2, status: "deployed", train_id: "train-1", verify_status: "succeeded" },
    ],
  });
  const deploymentAlerts = notificationCandidates(validated, deployed);
  assert.equal(deploymentAlerts.length, 1);
  assert.equal(deploymentAlerts[0].kind, "deployed");
  assert.equal(deploymentAlerts[0].body, "Train landed (2 jobs).");
});

test("new attention jobs aggregate without exposing notes", () => {
  const before = snapshot({
    jobs: [
      { id: 7, status: "in_progress" },
      { id: 8, status: "in_progress" },
    ],
  });
  const after = snapshot({
    selection: "idle",
    jobs: [
      { id: 7, status: "blocked", note: "token=secret-value" },
      { id: 8, status: "failed", note: "/private/worktree failed" },
    ],
  });
  const alerts = notificationCandidates(before, after);
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].kind, "attention");
  assert.match(alerts[0].body, /#7 blocked, #8 failed/);
  assert.doesNotMatch(alerts[0].body, /secret-value|private/);
  assert.doesNotMatch(alerts[0].id, /secret-value|private/);
  assert.deepEqual(notificationCandidates(after, after), []);
});

test("hub notifications target the repository drill-down", () => {
  const beforeRepo = snapshot({ jobs: [{ id: 3, status: "in_progress" }] });
  const afterRepo = snapshot({
    selection: "validated",
    jobs: [{ id: 3, status: "validated", train_id: "train-hub" }],
    trainId: "train-hub",
  });
  const previous = {
    ok: true,
    hub: true,
    repos: [{ path: "/work/api", name: "api", ok: true, empty: false, snapshot: beforeRepo }],
  };
  const current = {
    ok: true,
    hub: true,
    repos: [{ path: "/work/api", name: "api", ok: true, empty: false, snapshot: afterRepo }],
  };
  const [alert] = notificationCandidates(previous, current);
  assert.equal(alert.repoPath, "/work/api");
  assert.equal(alert.title, "mergetrain · api");
});

test("browser preference and cross-tab delivery claims persist safely", () => {
  const localStorage = storage();
  assert.equal(readNotificationPreference(localStorage), false);
  writeNotificationPreference(localStorage, true);
  assert.equal(readNotificationPreference(localStorage), true);
  assert.equal(claimNotification(localStorage, "validated:api:train-1", 1000), true);
  assert.equal(claimNotification(localStorage, "validated:api:train-1", 1001), false);
  assert.equal(claimNotification(localStorage, "validated:api:train-1", 301002), true);
  writeNotificationPreference(localStorage, false);
  assert.equal(readNotificationPreference(localStorage), false);
});
