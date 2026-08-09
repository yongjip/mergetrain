const ATTENTION_STATUSES = new Set([
  "blocked",
  "failed",
  "needs_reconcile",
  "deployed_verify_unknown",
]);

export const NOTIFICATION_PREFERENCE_KEY = "mergetrain-dashboard-notifications";
export const NOTIFICATION_SEEN_KEY = "mergetrain-dashboard-notifications-seen";
export const NOTIFICATION_DEDUP_MS = 5 * 60 * 1000;

function stableToken(value) {
  let hash = 2166136261;
  for (const character of String(value)) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function jobMap(snapshot = {}) {
  return new Map((snapshot.jobs || []).map((job) => [String(job.id), job]));
}

function isAttentionJob(job = {}) {
  return ATTENTION_STATUSES.has(job.status)
    || (job.status === "deployed" && ["failed", "unknown"].includes(job.verify_status));
}

function attentionIdentity(job = {}) {
  return [job.status, job.verify_status].filter(Boolean).join(":");
}

function projectName(snapshot = {}, fallback = "repository") {
  return snapshot.project?.name || fallback;
}

function repoIdentity(snapshot = {}, repoPath = "") {
  return stableToken(repoPath || projectName(snapshot));
}

function eligibleValidatedTrain(snapshot = {}) {
  return (snapshot.validated_trains || []).find((train) => train.deploy_eligible)
    || snapshot.validated_trains?.[0]
    || null;
}

function validatedIdentity(snapshot = {}) {
  if (snapshot.train?.selection !== "validated") return "";
  const train = eligibleValidatedTrain(snapshot);
  if (train?.train_id) return `train:${train.train_id}`;
  const jobs = snapshot.train?.jobs || [];
  const ids = jobs.map((job) => job.id).sort((a, b) => Number(a) - Number(b));
  const heads = jobs.map((job) => job.validated_head_sha || job.head_sha).filter(Boolean);
  return ids.length ? `jobs:${ids.join(",")}:${heads.join(",")}` : "";
}

function notificationTitle(snapshot, fallbackName) {
  return `mergetrain · ${projectName(snapshot, fallbackName)}`;
}

function repositoryCandidates(previous, current, { repoPath = "", fallbackName = "" } = {}) {
  if (!previous?.ok || !current?.ok) return [];
  const candidates = [];
  const key = repoIdentity(current, repoPath);
  const title = notificationTitle(current, fallbackName);
  const target = repoPath ? { repoPath } : {};
  const previousJobs = jobMap(previous);
  const currentJobs = current.jobs || [];

  const attention = currentJobs.filter((job) => {
    if (!isAttentionJob(job)) return false;
    const prior = previousJobs.get(String(job.id));
    return !prior || attentionIdentity(prior) !== attentionIdentity(job);
  });
  if (attention.length) {
    const summary = attention
      .slice(0, 3)
      .map((job) => `#${job.id} ${job.status === "deployed" ? "verification" : job.status}`)
      .join(", ");
    const extra = attention.length > 3 ? `, +${attention.length - 3} more` : "";
    const identity = attention
      .map((job) => `${job.id}:${attentionIdentity(job)}`)
      .sort()
      .join("|");
    candidates.push({
      id: `attention:${key}:${identity}`,
      title,
      body: `${attention.length} job${attention.length === 1 ? "" : "s"} need attention: ${summary}${extra}`,
      kind: "attention",
      ...target,
    });
  }

  const currentValidated = validatedIdentity(current);
  if (currentValidated && currentValidated !== validatedIdentity(previous)) {
    const count = current.train?.jobs?.length || eligibleValidatedTrain(current)?.job_ids?.length || 1;
    candidates.push({
      id: `validated:${key}:${stableToken(currentValidated)}`,
      title,
      body: `Train passed validation and is awaiting deploy approval (${count} job${count === 1 ? "" : "s"}).`,
      kind: "validated",
      ...target,
    });
  }

  const deployedGroups = new Map();
  currentJobs.forEach((job) => {
    const prior = previousJobs.get(String(job.id));
    if (job.status !== "deployed" || prior?.status === "deployed" || isAttentionJob(job)) return;
    const group = job.train_id || job.deploy_sha || `job-${job.id}`;
    const jobs = deployedGroups.get(group) || [];
    jobs.push(job);
    deployedGroups.set(group, jobs);
  });
  deployedGroups.forEach((jobs, group) => {
    candidates.push({
      id: `deployed:${key}:${stableToken(group)}`,
      title,
      body: `Train landed (${jobs.length} job${jobs.length === 1 ? "" : "s"}).`,
      kind: "deployed",
      ...target,
    });
  });

  return candidates;
}

export function notificationCandidates(previous, current) {
  if (!previous || !current?.ok) return [];
  if (current.hub) {
    if (!previous.hub) return [];
    const previousRepos = new Map(
      (previous.repos || []).map((entry) => [entry.path, entry]),
    );
    return (current.repos || []).flatMap((entry) => {
      const prior = previousRepos.get(entry.path);
      if (!prior) return [];
      const fallbackName = entry.name || entry.path;
      if (!entry.ok) {
        if (!prior.ok && prior.error === entry.error) return [];
        return [{
          id: `repo-error:${stableToken(entry.path)}`,
          title: `mergetrain · ${fallbackName}`,
          body: "Dashboard cannot read this repository. Open it to inspect the error.",
          kind: "attention",
          repoPath: entry.path,
        }];
      }
      if (entry.empty || !prior.ok || prior.empty) return [];
      return repositoryCandidates(prior.snapshot, entry.snapshot, {
        repoPath: entry.path,
        fallbackName,
      });
    });
  }
  if (previous.hub) return [];
  return repositoryCandidates(previous, current);
}

export function readNotificationPreference(storage) {
  try {
    return storage?.getItem(NOTIFICATION_PREFERENCE_KEY) === "enabled";
  } catch {
    return false;
  }
}

export function writeNotificationPreference(storage, enabled) {
  try {
    if (enabled) storage?.setItem(NOTIFICATION_PREFERENCE_KEY, "enabled");
    else storage?.removeItem(NOTIFICATION_PREFERENCE_KEY);
  } catch {
    // Browser privacy modes may deny storage. The current tab can still notify.
  }
}

function readSeen(storage) {
  try {
    const value = JSON.parse(storage?.getItem(NOTIFICATION_SEEN_KEY) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

export function claimNotification(storage, id, now = Date.now()) {
  const seen = readSeen(storage);
  const cutoff = now - NOTIFICATION_DEDUP_MS;
  const recent = Object.fromEntries(
    Object.entries(seen)
      .filter(([, timestamp]) => Number(timestamp) >= cutoff)
      .sort(([, a], [, b]) => Number(b) - Number(a))
      .slice(0, 199),
  );
  if (Object.hasOwn(recent, id)) return false;
  recent[id] = now;
  try {
    storage?.setItem(NOTIFICATION_SEEN_KEY, JSON.stringify(recent));
  } catch {
    // Fail open: a storage restriction should not silently suppress an alert.
  }
  return true;
}
