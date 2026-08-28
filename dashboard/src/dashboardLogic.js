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
  recover_stranded_claim: ["Recover work claimed by a runner that is gone.", "Run mergetrain recover: the next deploy would otherwise requeue it and clear its approved train identity."],
  initialize_config: ["Scaffold the repository config.", "Run mergetrain init: queue commands refuse to ship against guessed defaults."],
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

export function queuedAfterCurrentBatch(snapshot = {}, currentJobs = []) {
  const selection = snapshot.train?.selection;
  if (!["running", "validated"].includes(selection)) return [];
  const currentIds = new Set(currentJobs.map((job) => String(job.id)));
  return (snapshot.jobs || [])
    .filter((job) => job.status === "queued" && !currentIds.has(String(job.id)))
    .sort((a, b) => Number(a.id) - Number(b.id));
}

export function workspaceStepForSnapshot(snapshot = {}) {
  const selection = snapshot.train?.selection;
  if (selection === "validated") return 6;
  if (selection !== "running") return 0;

  const phase = snapshot.progress?.phase;
  if (["gating"].includes(phase)) return 5;
  if (["ready", "pushing", "verifying", "complete"].includes(phase)) return 6;
  if (phase === "assembling") {
    const mergedCount = snapshot.progress?.completed_job_ids?.length || 0;
    const trainSize = snapshot.train?.jobs?.length || 1;
    return Math.max(1, Math.min(trainSize, mergedCount + 1));
  }
  return 0;
}

export function repoStateForEntry(entry = {}) {
  if (!entry.ok) return ["error", "ERROR"];
  if (entry.empty) return ["waiting", "NO QUEUE"];

  const snapshot = entry.snapshot || {};
  const counts = snapshot.counts || {};
  if (counts.needs_reconcile || counts.blocked || counts.failed || counts.deployed_verify_unknown) {
    return ["warning", "ATTENTION"];
  }
  if (snapshot.lock?.liveness === "alive" || counts.in_progress) return ["active", "RUNNING"];
  if ((snapshot.validated_trains || []).some((train) => train.deploy_eligible)) {
    return ["approval", "APPROVAL"];
  }
  if (counts.queued) return ["queued", "QUEUED"];
  return ["idle", "IDLE"];
}

export function jobActivityAt(job = {}) {
  if (!job) return "";
  return job.finished_at
    || job.validated_at
    || job.started_at
    || job.requested_at
    || "";
}

export function latestRepoJob(jobs = []) {
  return [...jobs].sort((a, b) => {
    const timeDelta = Date.parse(jobActivityAt(b)) - Date.parse(jobActivityAt(a));
    if (Number.isFinite(timeDelta) && timeDelta !== 0) return timeDelta;
    return Number(b.id || 0) - Number(a.id || 0);
  })[0] || null;
}

export function etaRemainingSeconds(eta = {}, now = Date.now()) {
  if (!eta.available || !eta.expected_at) return null;
  const expected = Date.parse(eta.expected_at);
  if (!Number.isFinite(expected)) return null;
  return Math.max(0, Math.round((expected - now) / 1000));
}

export function gateWaterfallModel(eta = {}) {
  const gates = eta.gates || [];
  const maximum = Math.max(
    0,
    ...gates.map((gate) => Number(gate.median_seconds) || 0),
  );
  return gates.map((gate) => ({
    ...gate,
    widthPercent: maximum
      ? Math.max(8, Math.round(((Number(gate.median_seconds) || 0) / maximum) * 100))
      : 8,
  }));
}

const BROWSER_STATES = {
  attention: { color: "#d1242f", glyph: "🔴" },
  running: { color: "#0867ed", glyph: "🔵" },
  ready: { color: "#b06800", glyph: "🟠" },
  idle: { color: "#778195", glyph: "⚪" },
};

export function browserIndicator(snapshot = {}) {
  if (snapshot.hub) {
    const attention = (snapshot.repos || []).filter((entry) => {
      const [state] = repoStateForEntry(entry);
      return ["error", "warning"].includes(state);
    }).length;
    const state = attention ? "attention" : "idle";
    return { state, count: attention, label: "hub", ...BROWSER_STATES[state] };
  }
  const counts = snapshot.counts || {};
  const failures = (counts.blocked || 0)
    + (counts.failed || 0)
    + (counts.needs_reconcile || 0)
    + (counts.deployed_verify_unknown || 0);
  const state = failures
    ? "attention"
    : snapshot.train?.selection === "running"
      ? "running"
      : snapshot.train?.selection === "validated"
        ? "ready"
        : "idle";
  return {
    state,
    count: failures,
    label: state,
    ...BROWSER_STATES[state],
  };
}

export function stateFavicon(indicator) {
  const count = Math.min(99, Number(indicator.count) || 0);
  const badge = count
    ? `<circle cx="48" cy="16" r="14" fill="#ffffff"/><text x="48" y="21" text-anchor="middle" font-size="16" font-family="system-ui" font-weight="800" fill="${indicator.color}">${count}</text>`
    : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="27" fill="${indicator.color}"/><circle cx="32" cy="32" r="13" fill="#ffffff" fill-opacity=".92"/>${badge}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

export function splitJobIds(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function jobLabel(job) {
  if (job.task) return job.task;
  return String(job.branch || "pending job")
    .split("/")
    .at(-1)
    .replaceAll("-", " ");
}

export function sameBatch(job, selectedJobs) {
  const selectedIds = new Set(selectedJobs.map((item) => String(item.id)));
  if (selectedIds.has(String(job.id))) return true;
  const claimTokens = new Set(selectedJobs.map((item) => item.claim_token).filter(Boolean));
  const trainIds = new Set(selectedJobs.map((item) => item.train_id).filter(Boolean));
  const startedTimes = new Set(selectedJobs.map((item) => item.started_at).filter(Boolean));
  return (job.claim_token && claimTokens.has(job.claim_token))
    || (job.train_id && trainIds.has(job.train_id))
    || (job.started_at && startedTimes.has(job.started_at));
}

export function currentTrainModel(snapshot) {
  const jobMap = new Map();
  [...(snapshot.jobs || []), ...(snapshot.train.jobs || [])].forEach((job) => {
    jobMap.set(String(job.id), job);
  });
  const selection = snapshot.train.selection;
  const selectedJobs = [...(snapshot.train.jobs || [])]
    .sort((a, b) => Number(a.id) - Number(b.id));

  const validatedTrain = (snapshot.validated_trains || []).find((train) => train.deploy_eligible)
    || snapshot.validated_trains?.[0]
    || null;
  (validatedTrain?.branches || []).forEach((branch) => {
    const key = String(branch.job_id);
    if (!jobMap.has(key)) {
      jobMap.set(key, {
        id: branch.job_id,
        branch: branch.branch,
        validated_head_sha: branch.validated_head_sha,
        status: "validated",
      });
    }
  });

  const validatedIds = new Set([
    ...(validatedTrain?.job_ids || []),
    ...(validatedTrain?.branches || []).map((branch) => branch.job_id),
  ].map(String));
  const readyJobs = [...validatedIds]
    .map((id) => jobMap.get(id))
    .filter(Boolean)
    .sort((a, b) => Number(a.id) - Number(b.id));

  const attentionJobs = [...jobMap.values()]
    .filter((job) => ["blocked", "failed", "needs_reconcile"].includes(job.status))
    .sort((a, b) => Number(a.id) - Number(b.id));
  const batchAttentionJobs = selectedJobs.length
    ? attentionJobs.filter((job) => sameBatch(job, selectedJobs))
    : attentionJobs;
  const blockedJobs = ["running", "validated"].includes(selection)
    ? batchAttentionJobs
    : selection === "idle"
      ? attentionJobs
      : [];
  const safeJobs = selection === "validated"
    ? selectedJobs.filter((job) => job.status === "validated")
    : [];
  const currentJobs = [...new Map(
    [...selectedJobs, ...blockedJobs].map((job) => [String(job.id), job]),
  ).values()].sort((a, b) => Number(a.id) - Number(b.id));
  const nextBatchJobs = queuedAfterCurrentBatch(snapshot, currentJobs);

  return {
    attentionJobs,
    blockedJobs,
    safeJobs,
    readyJobs,
    currentJobs,
    nextBatchJobs,
    selection,
    validatedTrain,
  };
}

export function isGitConflict(job) {
  return splitJobIds(job.conflict_with).length === 0
    && String(job.note || "").toLowerCase().includes("conflict");
}

export function blockedReason(job) {
  if (isGitConflict(job)) return "Git conflict";
  if (splitJobIds(job.conflict_with).length) return "Semantic conflict";
  if (job.status === "needs_reconcile") return "Needs reconcile";
  return "Blocked";
}

export function contextualInspectorState(snapshot, demoStep, model = currentTrainModel(snapshot)) {
  const { attentionJobs } = model;
  const step = demoStep ?? workspaceStepForSnapshot(snapshot);
  return {
    blockedJobs: step >= 2 ? attentionJobs : [],
  };
}

export function conflictFiles(job) {
  return [...String(job.note || "").matchAll(/Merge conflict in ([^\n]+)/gi)]
    .map((match) => match[1].trim())
    .filter((path, index, paths) => path && paths.indexOf(path) === index)
    .slice(0, 3);
}

export function historyState(status) {
  if (["failed", "blocked", "needs_reconcile", "deployed_verify_unknown"].includes(status)) return "failed";
  if (["in_progress", "running"].includes(status)) return "active";
  if (["queued", "canceled"].includes(status)) return "queued";
  return "success";
}

export function repoOperationalCopy(entry, snapshot, state, words) {
  if (!entry.ok) {
    return { title: "Configuration error", detail: entry.error };
  }
  if (entry.empty) {
    return {
      title: "Queue not initialized",
      detail: "Enqueue the first committed task branch in this repository.",
    };
  }
  if (state === "approval") {
    return { title: "Awaiting deploy approval", detail: "Tests passed · not on main yet" };
  }
  if (state === "active") {
    const gate = snapshot.progress?.current_gate;
    return {
      title: snapshot.progress?.phase === "gating" ? "Running tests" : "Batch in progress",
      detail: gate
        ? `Gate ${gate.index}/${gate.total} · ${gate.name}`
        : snapshot.progress?.message || "The runner is processing the current batch.",
    };
  }
  if (state === "warning") {
    return { title: "Needs attention", detail: actionCopy(snapshot.next_action, words)[0] };
  }
  if (state === "queued") {
    const count = snapshot.counts?.queued || 0;
    return {
      title: "Queued for validation",
      detail: `${count} request${count === 1 ? "" : "s"} waiting to start`,
    };
  }
  return { title: "Queue clear", detail: "No requests waiting" };
}
