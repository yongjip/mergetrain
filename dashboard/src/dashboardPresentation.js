import { DEFAULT_TERMINOLOGY } from "./dashboardFormatters.js";

const PHASES = [
  ["queue", "Queue"],
  ["claiming", "Claim"],
  ["fetching", "Fetch"],
  ["assembling", "Assemble"],
  ["gating", "Gates"],
  ["ready", "Ready"],
  ["pushing", "Push"],
  ["verifying", "Verify"],
];

export const STATE_LABELS = {
  active: "RUNNING",
  success: "COMPLETE",
  done: "COMPLETE",
  warning: "ATTENTION",
  reused: "REUSED",
  skipped: "SKIPPED",
  error: "FAILED",
  failed: "FAILED",
  queued: "WAITING",
  waiting: "WAITING",
  started: "STARTED",
  idle: "IDLE",
};

export const PHASE_LABELS = Object.fromEntries(PHASES.map(([key, label]) => [key, label.toUpperCase()]));

export function gateDescription(name = "", words = DEFAULT_TERMINOLOGY) {
  const normalized = name.toLowerCase();
  if (normalized === "diff-check") return "Checks the assembled Git diff for whitespace errors and conflict markers.";
  if (normalized.includes("e2e") || normalized.includes("integration")) return `Exercises the installed CLI across real validation, merge, Git ${words.noun}, and recovery workflows.`;
  if (normalized.includes("unit") || normalized === "test" || normalized === "tests") return "Runs the project's fast automated tests against the assembled train.";
  if (normalized.includes("package") || normalized.includes("build")) return "Confirms the project can be built and packaged from the assembled train.";
  if (normalized.includes("lint") || normalized.includes("format")) return "Checks source consistency before this train can move forward.";
  if (normalized.includes("security") || normalized.includes("audit")) return "Checks the assembled train for configured security policy violations.";
  return "Runs a project-defined safety check against the entire assembled train.";
}

export function eventDescription(event, jobCount, words = DEFAULT_TERMINOLOGY) {
  if (event.phase === "claiming") return `Reserved ${jobCount || "the selected"} job${jobCount === 1 ? "" : "s"} for one runner so no second process can ${words.action} the same work.`;
  if (event.phase === "fetching") return "Refreshed the integration baseline and prepared an isolated worktree for this run.";
  if (event.phase === "assembling") return event.state === "success"
    ? `Merged the selected branches into one isolated ${jobCount ? `${jobCount}-job` : "multi-job"} train.`
    : event.state === "started"
      ? "Started combining the selected branches in queue order before any gate ran."
      : "Combining the selected branches in queue order before any gate runs.";
  if (event.phase === "gating") {
    const gateName = event.message.match(/gate \d+\/\d+: (.+)$/)?.[1] || "";
    return gateDescription(gateName, words);
  }
  if (event.phase === "ready") return `The exact train identity is validated and waiting for explicit ${words.noun} approval.`;
  if (event.phase === "pushing") return "Atomically updating the configured remote refs with the validated train.";
  if (event.phase === "verifying") return `Checking the ${words.completed} refs after the atomic push completed.`;
  if (event.phase === "complete") return event.state === "warning"
    ? "The remote refs were pushed, but post-push verification still needs attention."
    : "The runner finished this train and released its lease.";
  return "A structured milestone emitted by the local mergetrain runner.";
}
