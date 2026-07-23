import { useEffect, useState } from "react";
import {
  ArrowRight,
  Broadcast,
  CalendarBlank,
  CaretDown,
  CheckCircle,
  Circle,
  Clock,
  Copy,
  FileCode,
  GitBranch,
  Heartbeat,
  HourglassHigh,
  Info,
  ListChecks,
  Moon,
  Play,
  Pulse,
  ShieldCheck,
  SpinnerGap,
  StackSimple,
  Sun,
  TerminalWindow,
  Timer,
  WarningCircle,
  WifiHigh,
  XCircle,
} from "@phosphor-icons/react";
import { REMEDIAL_ACTIONS, actionCopy, reconnectDelay } from "./dashboardLogic.js";

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

const DEFAULT_TERMINOLOGY = {
  action: "deploy",
  in_progress: "deploying",
  completed: "deployed",
  noun: "deployment",
};

function terminology(snapshot) {
  return snapshot.project.terminology || DEFAULT_TERMINOLOGY;
}

const STATE_LABELS = {
  active: "RUNNING",
  success: "COMPLETE",
  done: "COMPLETE",
  warning: "ATTENTION",
  reused: "REUSED",
  error: "FAILED",
  failed: "FAILED",
  queued: "WAITING",
  waiting: "WAITING",
  started: "STARTED",
  idle: "IDLE",
};

const PHASE_LABELS = Object.fromEntries(PHASES.map(([key, label]) => [key, label.toUpperCase()]));

function gateDescription(name = "", words = DEFAULT_TERMINOLOGY) {
  const normalized = name.toLowerCase();
  if (normalized === "diff-check") return "Checks the assembled Git diff for whitespace errors and conflict markers.";
  if (normalized.includes("e2e") || normalized.includes("integration")) return `Exercises the installed CLI across real validation, merge, Git ${words.noun}, and recovery workflows.`;
  if (normalized.includes("unit") || normalized === "test" || normalized === "tests") return "Runs the project's fast automated tests against the assembled train.";
  if (normalized.includes("package") || normalized.includes("build")) return "Confirms the project can be built and packaged from the assembled train.";
  if (normalized.includes("lint") || normalized.includes("format")) return "Checks source consistency before this train can move forward.";
  if (normalized.includes("security") || normalized.includes("audit")) return "Checks the assembled train for configured security policy violations.";
  return "Runs a project-defined safety check against the entire assembled train.";
}

function eventDescription(event, jobCount, words = DEFAULT_TERMINOLOGY) {
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

function parseTime(value) {
  const time = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(time) ? null : time;
}

function duration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

function relative(value, now) {
  const time = parseTime(value);
  if (!time) return "—";
  const delta = Math.round((now - time) / 1000);
  if (Math.abs(delta) < 2) return "just now";
  if (delta < 0) return `in ${duration(-delta)}`;
  return `${duration(delta)} ago`;
}

function clockTime(value) {
  const time = value instanceof Date ? value.getTime() : parseTime(value);
  if (!time) return "--:--:--";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(time);
}

function dateTime(value) {
  const time = parseTime(value);
  if (!time) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(time);
}

function shortSha(value) {
  return value ? value.slice(0, 7) : "pending";
}

function phaseIndex(phase) {
  if (phase === "complete") return PHASES.length;
  return Math.max(0, PHASES.findIndex(([key]) => key === phase));
}

function phaseState(key, index, snapshot) {
  const progress = snapshot.progress;
  const current = phaseIndex(progress.phase);
  if (key === "queue") {
    if (snapshot.train.selection === "queued") return "active";
    return snapshot.train.jobs.length ? "done" : "waiting";
  }
  if (index < current) return "done";
  if (index === current) {
    if (key === "gating" && progress.gates?.some((gate) => gate.state !== "success")) return "active";
    if (progress.state === "success") return "done";
    if (progress.state === "warning") return "warning";
    return progress.state === "queued" || progress.state === "idle" ? "waiting" : "active";
  }
  if (progress.completed_phases?.includes(key)) return "done";
  return "waiting";
}

function StatusIcon({ state, size = 22 }) {
  if (state === "done" || state === "success" || state === "reused") return <CheckCircle size={size} weight="fill" />;
  if (state === "active") return <SpinnerGap size={size} weight="bold" className="spin" />;
  if (state === "error") return <XCircle size={size} weight="fill" />;
  if (state === "warning") return <WarningCircle size={size} weight="fill" />;
  return <Circle size={size} weight="regular" />;
}

function Header({
  snapshot,
  connection,
  now,
  hub,
  repoName,
  theme,
  onToggleTheme,
  demoState,
  onPlayDemo,
}) {
  const generated = relative(snapshot.generated_at, now);
  const connectionLabel = connection === "live" ? "CONNECTED" : connection === "offline" ? "DISCONNECTED" : "POLLING";
  const preview = !hub && snapshot.project?.preview;
  return (
    <header className="topbar">
      <div className="brand"><StackSimple size={34} weight="bold" /><strong>mergetrain</strong>{hub && <span className="hub-badge">HUB</span>}</div>
      {hub ? (
        repoName
          ? <div className="context"><FileCode size={19} /><span>{repoName}</span></div>
          : <div className="context"><StackSimple size={19} /><span>{snapshot.repo_count} repo{snapshot.repo_count === 1 ? "" : "s"}</span></div>
      ) : (
        <>
          <div className="context"><FileCode size={19} /><span>{snapshot.project.name}</span></div>
          <div className="context"><GitBranch size={19} /><span>{snapshot.project.integration_ref}</span></div>
        </>
      )}
      <span className="local-badge">LOCAL</span>
      {preview && <span className="preview-badge">DEMO DATA</span>}
      <div className="topbar-spacer" />
      {preview && (
        <button
          className={`demo-play ${demoState?.playing ? "playing" : ""}`}
          type="button"
          onClick={onPlayDemo}
          aria-label={demoState?.playing ? `Playing demo step ${demoState.step + 1} of 7` : "Play demo"}
          disabled={demoState?.playing}
        >
          {demoState?.playing ? <SpinnerGap size={17} className="spin" /> : <Play size={17} weight="fill" />}
          <span>{demoState?.playing ? `Playing ${demoState.step + 1} / 7` : "Play demo"}</span>
        </button>
      )}
      <div className={`live ${connection}`}><span className="live-dot" />{connectionLabel}<small>· updated {generated}</small></div>
      <button
        className="theme-toggle"
        type="button"
        onClick={onToggleTheme}
        aria-label={`Use ${theme === "dark" ? "light" : "dark"} theme`}
        title={`Use ${theme === "dark" ? "light" : "dark"} theme`}
      >
        {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
      </button>
      <div className="context divider"><Clock size={19} /><span>{clockTime(now)}</span></div>
      <div className="context divider"><CalendarBlank size={19} /><span>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(now)}</span></div>
    </header>
  );
}

const COUNT_ITEMS = [
  ["queued", "Queued"],
  ["in_progress", "Running"],
  ["blocked", "Blocked"],
  ["validated", "Validated"],
  ["deployed", "Deployed"],
];

function CountsStrip({ counts = {} }) {
  return (
    <section className="counts-strip" aria-label="Queue counts">
      {COUNT_ITEMS.map(([key, label]) => (
        <div className={`count-stat ${key}`} key={key}>
          <strong>{counts[key] || 0}</strong>
          <span>{label}</span>
        </div>
      ))}
    </section>
  );
}

function RemediationBanner({ snapshot }) {
  if (!REMEDIAL_ACTIONS.has(snapshot.next_action)) return null;
  const [title, detail] = actionCopy(snapshot.next_action, terminology(snapshot));
  const severe = ["unlock_wedged_runner", "reconcile_pending_deploy", "reconcile_conflict_manual"].includes(snapshot.next_action);
  return (
    <section className={`remediation-banner ${severe ? "error" : "warning"}`} role="alert">
      <WarningCircle size={24} weight="fill" />
      <div><strong>{title}</strong><p>{detail}</p></div>
      <code>{snapshot.next_action}</code>
    </section>
  );
}

function PreviewBanner() {
  return (
    <div className="preview-banner" role="status">
      <Info size={18} weight="fill" />
      <strong>Preview data</strong>
      <span>Local walkthrough state for UI review. Replay changes presentation only.</span>
    </div>
  );
}

function Hero({ snapshot, now }) {
  const jobs = snapshot.train.jobs;
  const selection = snapshot.train.selection;
  const deploying = jobs.some((job) => job.status === "in_progress" && job.train_id);
  const words = terminology(snapshot);
  const operation = words.in_progress.charAt(0).toUpperCase() + words.in_progress.slice(1);
  const title = selection === "running"
    ? `${deploying ? operation : "Validating"} train · ${jobs.length} job${jobs.length === 1 ? "" : "s"}`
    : selection === "validated"
      ? `Train ready · ${jobs.length} job${jobs.length === 1 ? "" : "s"}`
      : selection === "queued"
        ? `Queued train · ${jobs.length} job${jobs.length === 1 ? "" : "s"}`
        : "No active train";
  const started = parseTime(snapshot.progress.started_at);
  const elapsed = started ? duration((now - started) / 1000) : "waiting";
  return (
    <section className="hero">
      <div className={`hero-icon ${snapshot.progress.state}`}><Broadcast size={34} weight="duotone" /></div>
      <div>
        <h1>{title}</h1>
        <p><span>{snapshot.progress.message}</span><i>·</i><strong>{elapsed}</strong></p>
      </div>
    </section>
  );
}

function PhaseRail({ snapshot }) {
  return (
    <section className="phase-rail" aria-label="Train phases">
      {PHASES.map(([key, label], index) => {
        const state = phaseState(key, index, snapshot);
        const gateCount = key === "gating" ? snapshot.project.gate_count : 0;
        return (
          <div className={`phase ${state}`} key={key}>
            <span className="phase-label">{label}</span>
            {gateCount > 0 && <span className="gate-count">{gateCount}</span>}
            <span className="phase-track"><StatusIcon state={state} size={28} /></span>
          </div>
        );
      })}
    </section>
  );
}

function CurrentWork({ snapshot, now }) {
  if (snapshot.train.selection !== "running") return null;
  const progress = snapshot.progress;
  const words = terminology(snapshot);
  const gate = progress.current_gate;
  const state = gate?.state || progress.state;
  const title = gate
    ? `Gate ${gate.index} of ${gate.total} · ${gate.name}`
    : progress.message;
  const description = gate
    ? gateDescription(gate.name, words)
    : eventDescription({ phase: progress.phase, state: progress.state, message: progress.message }, snapshot.train.jobs.length, words);
  const command = gate?.command || progress.detail;
  const stepStarted = parseTime(gate?.started_at || progress.updated_at);
  const elapsed = stepStarted ? duration((now - stepStarted) / 1000) : "—";
  const scope = progress.job_id ? `Job #${progress.job_id}` : `Entire train · ${snapshot.train.jobs.length} jobs`;

  return (
    <section className="current-work" aria-labelledby="current-work-title">
      <div className="current-work-heading">
        <span className="eyebrow"><TerminalWindow size={18} />Current check</span>
        <span className={`state-pill ${state}`}>{STATE_LABELS[state] || state.toUpperCase()}</span>
      </div>
      <div className="current-work-summary">
        <div>
          <h2 id="current-work-title">{title}</h2>
          <p>{description}</p>
        </div>
        <dl>
          <div><dt>Scope</dt><dd>{scope}</dd></div>
          <div><dt>Elapsed</dt><dd>{elapsed}</dd></div>
        </dl>
      </div>
      {command && <div className="current-command"><TerminalWindow size={17} /><code>{command}</code></div>}
      {!!progress.gates?.length && (
        <ol className="gate-list" aria-label="Configured gates">
          {progress.gates.map((item) => (
            <li className={item.state} key={item.index}>
              <StatusIcon state={item.state} size={18} />
              <span>{item.index}</span>
              <strong>{item.name}</strong>
              <small>{STATE_LABELS[item.state] || "WAITING"}</small>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function JobCards({ snapshot }) {
  const jobs = snapshot.train.jobs;
  if (!jobs.length) {
    return <div className="empty-train"><ShieldCheck size={28} weight="duotone" /><span>The track is clear. Enqueue a committed branch to start the next train.</span></div>;
  }
  return (
    <section className="job-track" aria-label="Jobs in selected train">
      {jobs.map((job, index) => {
        const active = snapshot.progress.job_id === job.id || (
          snapshot.train.selection === "running"
          && !snapshot.progress.job_id
          && phaseIndex(snapshot.progress.phase) <= phaseIndex("assembling")
          && index === 0
        );
        const assembled = snapshot.progress.completed_job_ids?.includes(job.id) || phaseIndex(snapshot.progress.phase) > phaseIndex("assembling");
        const state = job.status === "deployed" && job.verify_status === "failed"
          ? "warning"
          : ["validated", "deployed"].includes(job.status)
            ? "done"
            : ["blocked", "failed"].includes(job.status)
              ? "error"
              : active
                ? "active"
                : assembled
                  ? "done"
                  : "waiting";
        return (
          <article className={`job-card ${state}`} key={job.id}>
            <div className="job-card-head"><strong>#{job.id}</strong><StatusIcon state={state} size={21} /></div>
            <h3>{job.task}</h3>
            <code>{job.branch}</code>
            <footer><span>{index + 1} / {jobs.length}</span><small>{shortSha(job.head_sha || job.validated_head_sha)}</small></footer>
          </article>
        );
      })}
    </section>
  );
}

function CopyCommand({ value, label }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="copy-command">
      <div><small>{label}</small><code>{value}</code></div>
      <button type="button" onClick={copy} aria-label={`Copy ${label}`}><Copy size={17} />{copied ? "Copied" : "Copy"}</button>
    </div>
  );
}

function ReadyToDeploy({ snapshot }) {
  const trains = snapshot.validated_trains || [];
  if (!trains.length) return null;
  const words = terminology(snapshot);
  return (
    <section className="ready-panel" aria-labelledby="ready-panel-title">
      <div className="ready-heading">
        <div><span className="eyebrow"><ShieldCheck size={18} />Approval surface</span><h2 id="ready-panel-title">Ready to {words.action}</h2></div>
        <span className="read-only-badge">READ-ONLY</span>
      </div>
      {trains.map((train) => {
        const command = `mergetrain run-batch --deploy --train-id ${train.train_id}`;
        const guarded = `scripts/mt-deploy.sh --confirm --train-id ${train.train_id}`;
        return (
          <article className={`ready-train ${train.deploy_eligible ? "eligible" : "incomplete"}`} key={train.train_id || `legacy-${(train.job_ids || []).join("-")}`}>
            <div className="train-identity">
              <div><small>Train ID</small><code>{train.train_id || "missing legacy identity"}</code></div>
              <div className="identity-badges">
                <span className={`state-pill ${train.deploy_eligible ? "done" : "warning"}`}>{train.deploy_eligible ? "DEPLOY ELIGIBLE" : "IDENTITY INCOMPLETE"}</span>
                <span className={`state-pill ${train.reuse_identity_complete ? "done" : "waiting"}`}>{train.reuse_identity_complete ? "REUSE IDENTITY COMPLETE" : "GATES MUST RERUN"}</span>
              </div>
            </div>
            <div className="train-members">
              {(train.branches || []).map((branch) => (
                <div key={branch.job_id}>
                  <strong>#{branch.job_id}</strong>
                  <code>{branch.branch}</code>
                  <span>{shortSha(branch.validated_head_sha)}</span>
                </div>
              ))}
            </div>
            {train.deploy_eligible && (
              <div className="train-commands">
                <CopyCommand value={guarded} label="Guarded confirmation" />
                <CopyCommand value={command} label="Direct CLI" />
              </div>
            )}
          </article>
        );
      })}
    </section>
  );
}

function DeploymentHistory({ jobs, words }) {
  const trains = [];
  const bySha = new Map();
  jobs.filter((job) => job.status === "deployed").forEach((job) => {
    const key = job.deploy_sha || `job-${job.id}`;
    let train = bySha.get(key);
    if (!train) {
      train = { key, sha: job.deploy_sha, jobs: [], finished_at: job.finished_at, started_at: job.started_at };
      bySha.set(key, train);
      trains.push(train);
    }
    train.jobs.push(job);
  });
  if (!trains.length) return null;
  return (
    <section className="deployment-history" aria-labelledby="deployment-history-title">
      <div className="activity-heading"><h2 id="deployment-history-title">Recent {words.noun} history</h2><span>Newest trains · local queue record</span></div>
      <div className="history-list">
        {trains.slice(0, 5).map((train) => {
          const started = parseTime(train.started_at);
          const finished = parseTime(train.finished_at);
          const elapsed = started && finished ? duration((finished - started) / 1000) : "—";
          const verifyStates = [...new Set(train.jobs.map((job) => job.verify_status || "not_run"))];
          const warning = verifyStates.some((state) => ["failed", "unknown"].includes(state));
          return (
            <article className="history-row" key={train.key}>
              <div className={`history-status ${warning ? "warning" : "success"}`}><StatusIcon state={warning ? "warning" : "done"} size={22} /></div>
              <div className="history-copy">
                <strong>{train.jobs.length}-job train</strong>
                <div>{train.jobs.map((job) => <code key={job.id}>#{job.id} {job.branch}</code>)}</div>
              </div>
              <dl>
                <div><dt>Deploy</dt><dd><code>{shortSha(train.sha)}</code></dd></div>
                <div><dt>Verify</dt><dd>{verifyStates.join(", ")}</dd></div>
                <div><dt>Duration</dt><dd>{elapsed}</dd></div>
                <div><dt>Finished</dt><dd>{dateTime(train.finished_at)}</dd></div>
              </dl>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function Activity({ events, jobCount, words }) {
  const hasTrainAssembly = events.some((event) => event.phase === "assembling" && event.job_id === null);
  const visible = events
    .filter((event) => !(hasTrainAssembly && event.phase === "assembling" && event.job_id !== null))
    .slice(-5)
    .reverse();
  return (
    <section className="activity">
      <div className="activity-heading">
        <h2>Activity</h2>
        <span>Newest first · runner milestones</span>
      </div>
      {!visible.length && <p className="empty-copy">Runner events will appear here as the train moves.</p>}
      <div className="activity-list">
        {visible.map((event) => {
          const resolved = event.state === "active" && events.some((later) => (
            later.id > event.id
            && later.phase === event.phase
            && later.job_id === event.job_id
            && ["success", "warning", "error"].includes(later.state)
          ));
          const displayState = resolved ? "started" : event.state;
          const displayEvent = { ...event, state: displayState };
          return (
          <article className={`activity-row ${displayState}`} key={event.id}>
            <time>{clockTime(event.created_at)}</time>
            <span className="event-icon"><StatusIcon state={displayState === "success" ? "done" : displayState} size={19} /></span>
            <div className="event-copy">
              <div className="event-labels">
                <span className="phase-pill">{PHASE_LABELS[event.phase] || event.phase.toUpperCase()}</span>
                <span className={`state-pill ${displayState}`}>{STATE_LABELS[displayState] || displayState.toUpperCase()}</span>
              </div>
              <strong>{event.message}</strong>
              <p>{eventDescription(displayEvent, jobCount, words)}</p>
              {event.detail && (event.phase === "gating"
                ? <div className="event-command"><TerminalWindow size={15} /><code>{event.detail}</code></div>
                : <div className="event-detail"><span>DETAIL</span><code>{event.detail}</code></div>)}
            </div>
          </article>
          );
        })}
      </div>
    </section>
  );
}

function RunnerPanel({ snapshot, now }) {
  const lock = snapshot.lock;
  const alive = lock?.liveness === "alive";
  return (
    <section className="rail-section runner-section">
      <div className="rail-heading"><h2>Runner</h2><span className={`state-pill ${alive ? "active" : "idle"}`}>{alive ? "ACTIVE" : "IDLE"}</span></div>
      <dl>
        <div><dt><ListChecks size={22} />Owner</dt><dd><code>{lock?.owner || "—"}</code></dd></div>
        <div><dt><Heartbeat size={22} />Health</dt><dd className={alive ? "healthy" : "muted"}>{alive ? "Healthy" : "Idle"}</dd></div>
        <div><dt><Pulse size={22} />Heartbeat</dt><dd className={alive ? "healthy" : "muted"}>{lock ? relative(lock.heartbeat_at, now) : "—"}</dd></div>
        <div><dt><Timer size={22} />Lease expires</dt><dd className="attention">{lock ? relative(lock.expires_at, now) : "—"}</dd></div>
      </dl>
    </section>
  );
}

function AttentionPanel({ jobs }) {
  const problemJobs = jobs.filter((item) => (
    item.status === "blocked"
    || item.status === "failed"
    || item.status === "needs_reconcile"
    || (item.status === "deployed" && item.verify_status === "failed")
  ));
  return (
    <section className="rail-section blocked-section">
      <h2>Attention <small>(history)</small></h2>
      {problemJobs.length ? (
        <div className="blocked-list">
          {problemJobs.map((job) => {
            const verifyWarning = job.status === "deployed" && job.verify_status === "failed";
            return (
              <article className="blocked-item" key={job.id}>
                <div className={`blocked-title ${verifyWarning ? "warning" : "error"}`}>
                  {verifyWarning
                    ? <WarningCircle size={24} weight="fill" />
                    : <XCircle size={24} weight="fill" />}
                  <strong>#{job.id}</strong><span>{job.task}</span>
                </div>
                <div className="blocked-detail">
                  <small>Reason</small><p>{job.note || "No reason recorded"}</p>
                  {job.conflict_with && <div className="conflict-badge"><GitBranch size={14} />conflicts with <code>{job.conflict_with}</code></div>}
                  <small>Occurred</small><code>{dateTime(job.finished_at || job.requested_at)}</code>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="clear-history"><CheckCircle size={24} weight="fill" /><span>No jobs need attention in recent history.</span></div>
      )}
    </section>
  );
}

function NextAction({ snapshot }) {
  const words = terminology(snapshot);
  const [title, detail] = actionCopy(snapshot.next_action, words);
  const targets = (snapshot.project.push_specs || []).join(", ");
  return (
    <section className="rail-section action-section">
      <h2>Next safe action</h2>
      <div className="action-title"><HourglassHigh size={29} weight="duotone" /><strong>{title}</strong></div>
      <p>{detail}</p>
      {targets && <p><code>{snapshot.project.remote}: {targets}</code></p>}
    </section>
  );
}

function Loading() {
  return <main className="loading"><SpinnerGap size={36} className="spin" /><strong>Reading local train state…</strong></main>;
}

const WORKSPACE_PHASES = [
  ["queue", "Queue"],
  ["merge", "Merge in order"],
  ["gate", "Gates"],
  ["ready", "Ready"],
];

function splitJobIds(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function jobLabel(job) {
  if (job.task) return job.task;
  return String(job.branch || "pending job")
    .split("/")
    .at(-1)
    .replaceAll("-", " ");
}

function currentTrainModel(snapshot) {
  const jobMap = new Map();
  [...(snapshot.jobs || []), ...(snapshot.train.jobs || [])].forEach((job) => {
    jobMap.set(String(job.id), job);
  });

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
  const safeJobs = [...validatedIds]
    .map((id) => jobMap.get(id))
    .filter(Boolean)
    .sort((a, b) => Number(a.id) - Number(b.id));

  const blockedJobs = [...jobMap.values()]
    .filter((job) => ["blocked", "failed", "needs_reconcile"].includes(job.status))
    .sort((a, b) => Number(a.id) - Number(b.id));
  const fallbackJobs = [...(snapshot.train.jobs || [])].sort((a, b) => Number(a.id) - Number(b.id));
  const currentJobs = blockedJobs.length || safeJobs.length
    ? [...new Map([...blockedJobs, ...safeJobs].map((job) => [String(job.id), job])).values()]
      .sort((a, b) => Number(a.id) - Number(b.id))
    : fallbackJobs;

  return {
    blockedJobs,
    safeJobs,
    currentJobs,
    validatedTrain,
  };
}

function isGitConflict(job) {
  return splitJobIds(job.conflict_with).length === 0
    && String(job.note || "").toLowerCase().includes("conflict");
}

function blockedReason(job) {
  if (isGitConflict(job)) return "Git conflict";
  if (splitJobIds(job.conflict_with).length) return "Semantic conflict";
  if (job.status === "needs_reconcile") return "Needs reconcile";
  return "Blocked";
}

function workspacePhaseState(index, step) {
  if (index === 0) return step === 0 ? "active" : "done";
  if (index === 1) return step < 1 ? "waiting" : step <= 4 ? "active" : "done";
  if (index === 2) return step < 5 ? "waiting" : step === 5 ? "active" : "done";
  return step < 6 ? "waiting" : "done";
}

function WorkspacePhaseRail({ step }) {
  return (
    <ol className="workspace-phase-rail" aria-label="Current train phases">
      {WORKSPACE_PHASES.map(([key, label], index) => {
        const state = workspacePhaseState(index, step);
        return (
          <li className={state} key={key}>
            <span>{label}</span>
            <i><StatusIcon state={state} size={23} /></i>
          </li>
        );
      })}
    </ol>
  );
}

function TrainJobRow({ job, blocked, order, step }) {
  const mergeReached = step >= order;
  const gateRunning = step === 5;
  const gatePassed = step >= 6;
  const mergeState = !mergeReached ? "waiting" : blocked ? "error" : "done";
  const gateState = blocked && mergeReached
    ? "waiting"
    : gatePassed
      ? "done"
      : gateRunning
        ? "active"
        : "waiting";
  const outcomeState = blocked && mergeReached
    ? "error"
    : gatePassed
      ? "done"
      : "waiting";

  return (
    <div className={`train-job-row ${blocked && mergeReached ? "blocked" : ""}`} role="row">
      <div className="job-cell order-cell" role="cell">
        <span>{order}</span>
      </div>
      <div className="job-cell identity-cell" role="cell">
        <strong>#{job.id}</strong>
        <div>
          <span>{jobLabel(job)}</span>
          <code>{job.branch || "branch pending"} · {shortSha(job.head_sha || job.validated_head_sha)}</code>
        </div>
      </div>
      <div className={`job-cell result-cell ${mergeState}`} role="cell">
        <StatusIcon state={mergeState} size={17} />
        <span>{!mergeReached ? "Waiting" : blocked ? blockedReason(job) : "Merged"}</span>
      </div>
      <div className={`job-cell result-cell ${gateState}`} role="cell">
        <StatusIcon state={gateState} size={17} />
        <span>
          {blocked && mergeReached
            ? "Skipped"
            : gatePassed
              ? "Passed"
              : gateRunning
                ? "Running"
                : "Waiting"}
        </span>
      </div>
      <div className={`job-cell outcome-cell ${outcomeState}`} role="cell">
        <span>
          {blocked && mergeReached
            ? "Rebase"
            : gatePassed
              ? "Train member"
              : mergeReached
                ? "Candidate"
                : "Queued"}
        </span>
      </div>
    </div>
  );
}

function FifoJobList({ jobs, blockedIds, step }) {
  if (!jobs.length) return null;
  const blockedCount = jobs.filter((job) => blockedIds.has(String(job.id))).length;
  return (
    <section className={`train-job-group fifo ${step >= 6 ? "resolved" : "pending"}`}>
      <header>
        <div>
          <ListChecks size={19} weight="fill" />
          <strong>FIFO merge order</strong>
          <span>{jobs.map((job) => `#${job.id}`).join(" → ")}</span>
        </div>
        <span>
          {step >= 6
            ? `${blockedCount} skipped · ${jobs.length - blockedCount} continue`
            : step >= 1
              ? "Merging one by one"
              : "Oldest request first"}
        </span>
      </header>
      <div role="rowgroup">
        {jobs.map((job, index) => (
          <TrainJobRow
            job={job}
            blocked={blockedIds.has(String(job.id))}
            order={index + 1}
            step={step}
            key={job.id}
          />
        ))}
      </div>
    </section>
  );
}

function CurrentTrainWorkspace({ snapshot, demoStep }) {
  const { blockedJobs, safeJobs, currentJobs, validatedTrain } = currentTrainModel(snapshot);
  const step = demoStep ?? 6;
  const blockedIds = new Set(blockedJobs.map((job) => String(job.id)));
  const safeNames = safeJobs.map((job) => `#${job.id}`).join(" + ");
  const status = step === 0
    ? `${currentJobs.length} requests queued`
    : step <= 4
      ? `Merging request ${Math.min(step, currentJobs.length)} of ${currentJobs.length}`
      : step === 5
        ? "Running gates"
        : safeJobs.length
          ? `${safeJobs.length} requests validated`
          : "Ready";

  return (
    <section className="current-train-card" aria-labelledby="current-train-title">
      <header className="current-train-heading">
        <div>
          <span className="workspace-eyebrow">Current train</span>
          <h1 id="current-train-title">FIFO train · {currentJobs.length} request{currentJobs.length === 1 ? "" : "s"}</h1>
        </div>
        <div className="workspace-status-list">
          {blockedJobs.length > 0 && step >= 2 && (
            <span className="workspace-status error">
              <StatusIcon state="error" size={18} />
              {blockedJobs.length} blocked
            </span>
          )}
          {safeJobs.length > 0 && step >= 6 && (
            <span className="workspace-status done">
              <StatusIcon state="done" size={18} />
              {safeJobs.length} validated
            </span>
          )}
          {!(blockedJobs.length > 0 && step >= 2) && !(safeJobs.length > 0 && step >= 6) && (
            <span className="workspace-status active">
              <StatusIcon state="active" size={18} />
              {status}
            </span>
          )}
        </div>
      </header>

      <WorkspacePhaseRail step={step} />

      <div className="train-table" role="table" aria-label="FIFO merge requests and train outcomes">
        <div className="train-table-head" role="row">
          <span role="columnheader">Order</span>
          <span role="columnheader">Merge request</span>
          <span role="columnheader">FIFO merge</span>
          <span role="columnheader">Gate</span>
          <span role="columnheader">Outcome</span>
        </div>
        <FifoJobList jobs={currentJobs} blockedIds={blockedIds} step={step} />
      </div>

      {!!safeJobs.length && (
        <div className={`validated-train-summary ${step >= 6 ? "ready" : ""}`}>
          <CheckCircle size={21} weight="fill" />
          <div>
            <span>Validated train</span>
            <strong>{safeNames}</strong>
          </div>
          <ArrowRight size={18} />
          <code>main after approval</code>
        </div>
      )}

      <footer className="train-meta">
        <span>Train ID</span>
        <code>{validatedTrain?.train_id || snapshot.train.jobs?.[0]?.train_id || "assigned after validation"}</code>
        <span className="train-meta-spacer" />
        <span>Updated</span>
        <time>{relative(snapshot.generated_at, new Date())}</time>
      </footer>
    </section>
  );
}

function WhatHappened({ snapshot, demoStep }) {
  const { blockedJobs, safeJobs, currentJobs } = currentTrainModel(snapshot);
  const step = demoStep ?? 6;
  const first = currentJobs[0];
  const blocked = blockedJobs[0];
  const later = currentJobs.filter((job) => (
    first
    && blocked
    && Number(job.id) > Number(blocked.id)
    && !blockedJobs.some((item) => String(item.id) === String(job.id))
  ));
  const firstName = first ? `#${first.id}` : "The first request";
  const blockedName = blocked ? `#${blocked.id}` : "The next request";
  const laterNames = later.map((job) => `#${job.id}`).join(" and ");
  const safeNames = safeJobs.map((job) => `#${job.id}`).join(" + ");
  const facts = [
    step === 0
      ? `${currentJobs.length} committed merge requests entered in FIFO order.`
      : `${firstName} merged first because it was oldest in the queue.`,
    step < 2
      ? `${blockedName} is next. Each request merges onto the candidate built so far.`
      : blocked
        ? `${blockedName} hit a ${blockedReason(blocked).toLowerCase()} and was skipped.`
        : "Every request merged cleanly in queue order.",
    step < 4
      ? "Later requests keep their place and continue after a skipped conflict."
      : step < 6
        ? `${laterNames || "Later requests"} continued; gates now check the surviving train together.`
        : `${safeNames} passed gates together and can update main atomically.`,
  ];

  return (
    <section className="inspector-section what-happened">
      <div className="inspector-heading"><h2>What happened</h2>{blockedJobs.length ? <XCircle size={20} weight="fill" /> : <CheckCircle size={20} weight="fill" />}</div>
      <ol>
        {facts.map((fact, index) => <li key={fact}><span>{index + 1}</span><p>{fact}</p></li>)}
      </ol>
    </section>
  );
}

function NextSafeActions({ snapshot, demoStep }) {
  const { blockedJobs, safeJobs } = currentTrainModel(snapshot);
  const ready = (demoStep ?? 6) >= 6;
  return (
    <section className="inspector-section next-safe-actions">
      <h2>Next safe action</h2>
      {!!safeJobs.length && (
        <div className={`safe-action ready ${ready ? "" : "muted"}`}>
          <CheckCircle size={19} weight="fill" />
          <div>
            <strong>Deploy {safeJobs.map((job) => `#${job.id}`).join(" + ")} together</strong>
            <p>{ready ? "One approved atomic update to main." : "Wait for the combined gates to finish."}</p>
          </div>
        </div>
      )}
      {!!blockedJobs.length && (
        <div className="safe-action repair">
          <XCircle size={19} weight="fill" />
          <div>
            <strong>Rebase {blockedJobs.map((job) => `#${job.id}`).join(" + ")} on latest main</strong>
            <p>Commit the fix, then enqueue a fresh request.</p>
          </div>
        </div>
      )}
    </section>
  );
}

function CollapsedDetails({ snapshot, now }) {
  const events = (snapshot.events || []).slice(-4).reverse();
  const lock = snapshot.lock;
  return (
    <details className="inspector-section inspector-details">
      <summary><span>Logs and runner details</span><small>Collapsed</small><CaretDown size={17} /></summary>
      <div className="runner-compact">
        <span>Runner</span><strong>{lock?.liveness === "alive" ? "Active" : "Idle"}</strong>
        <span>Heartbeat</span><code>{lock ? relative(lock.heartbeat_at, now) : "—"}</code>
      </div>
      <div className="compact-event-list">
        {events.map((event) => (
          <div key={event.id}>
            <StatusIcon state={event.state === "success" ? "done" : event.state} size={16} />
            <span>{event.message}</span>
            <time>{clockTime(event.created_at)}</time>
          </div>
        ))}
        {!events.length && <p>No runner events yet.</p>}
      </div>
    </details>
  );
}

function TrainInspector({ snapshot, now, demoStep }) {
  return (
    <aside className="train-inspector" aria-label="Current train explanation and next actions">
      <WhatHappened snapshot={snapshot} demoStep={demoStep} />
      <NextSafeActions snapshot={snapshot} demoStep={demoStep} />
      <CollapsedDetails snapshot={snapshot} now={now} />
      {snapshot.project.preview && (
        <section className="inspector-section demo-note">
          <strong>Demo data</strong>
          <p>Real local walkthrough data. Replay changes presentation only.</p>
        </section>
      )}
    </aside>
  );
}

function SingleRepoBody({ snapshot, now, demoStep }) {
  const recentJobs = snapshot.jobs || [];
  const words = terminology(snapshot);
  return (
    <main className="workspace-shell">
      <div className="train-workspace-grid">
        <CurrentTrainWorkspace snapshot={snapshot} demoStep={demoStep} />
        <TrainInspector snapshot={snapshot} now={now} demoStep={demoStep} />
      </div>
      <details className="secondary-drawer">
        <summary><span>Full activity and history</span><small>Operational detail</small><CaretDown size={18} /></summary>
        <div className="secondary-grid">
          <Activity events={snapshot.events} jobCount={snapshot.train.jobs.length} words={words} />
          <div>
            <RunnerPanel snapshot={snapshot} now={now} />
            <AttentionPanel jobs={recentJobs} />
          </div>
        </div>
        <DeploymentHistory jobs={recentJobs} words={words} />
      </details>
    </main>
  );
}

const REPO_CARD_COUNTS = [
  ["queued", "queued"],
  ["in_progress", "running"],
  ["blocked", "blocked"],
  ["failed", "failed"],
  ["needs_reconcile", "reconcile"],
  ["validated", "validated"],
];

function repoCardState(entry) {
  if (!entry.ok) return ["error", "ERROR"];
  if (entry.empty) return ["waiting", "NO QUEUE"];
  const snapshot = entry.snapshot;
  const c = snapshot.counts || {};
  if (c.needs_reconcile || c.blocked || c.failed || c.deployed_verify_unknown) return ["warning", "ATTENTION"];
  if (snapshot.lock?.liveness === "alive" || c.in_progress) return ["active", "RUNNING"];
  if ((snapshot.validated_trains || []).some((train) => train.deploy_eligible)) return ["done", "READY"];
  return ["idle", "IDLE"];
}

function RepoCard({ entry, onSelect, now }) {
  const [state, label] = repoCardState(entry);
  const name = entry.name || entry.path;
  const snapshot = entry.ok && !entry.empty ? entry.snapshot : null;
  const chips = snapshot
    ? REPO_CARD_COUNTS.filter(([key]) => snapshot.counts?.[key]).map(([key, text]) => (
        <span className={`count-chip ${key}`} key={key}>{snapshot.counts[key]} {text}</span>
      ))
    : [];
  if (entry.daemon === false) {
    chips.push(<span className="count-chip daemon-off" key="daemon-off">daemon off</span>);
  }
  const words = snapshot ? terminology(snapshot) : DEFAULT_TERMINOLOGY;
  const summary = !entry.ok
    ? entry.error
    : entry.empty
      ? "No queue database yet — enqueue the first job in this repo."
      : actionCopy(snapshot.next_action, words)[0];
  const clickable = Boolean(snapshot);
  return (
    <article
      className={`repo-card ${state} ${clickable ? "clickable" : ""}`}
      onClick={clickable ? () => onSelect(entry.path) : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (event) => { if (event.key === "Enter" || event.key === " ") onSelect(entry.path); } : undefined}
    >
      <div className="repo-card-head">
        <strong>{name}</strong>
        <span className={`state-pill ${state}`}>{label}</span>
      </div>
      <code className="repo-path">{entry.path}</code>
      {!!chips.length && <div className="repo-chips">{chips}</div>}
      <p className="repo-summary">
        {!entry.ok && <WarningCircle size={17} weight="fill" />}
        <span>{summary}</span>
      </p>
      {snapshot && (
        <footer className="repo-card-foot">
          <span><GitBranch size={15} />{snapshot.project.integration_ref}</span>
          <span><Heartbeat size={15} />{snapshot.lock ? relative(snapshot.lock.heartbeat_at, now) : "idle"}</span>
        </footer>
      )}
    </article>
  );
}

function RegistryErrorBanner({ message }) {
  return (
    <div className="registry-error-banner" role="alert">
      <WarningCircle size={18} weight="fill" />
      <strong>Registry unreadable</strong>
      <span>{message}</span>
    </div>
  );
}

const REPO_SEVERITY = { error: 0, warning: 1, active: 2, done: 3, waiting: 4, idle: 5 };

function HubOverview({ snapshot, onSelect, now }) {
  if (!snapshot.repos.length) {
    return (
      <main className="hub-empty">
        <StackSimple size={30} weight="duotone" />
        <strong>No repos registered.</strong>
        <span>Run <code>mergetrain hub add &lt;repo&gt;</code> to put a repo on this board.</span>
      </main>
    );
  }
  const repos = [...snapshot.repos].sort((a, b) => {
    const [aState] = repoCardState(a);
    const [bState] = repoCardState(b);
    return REPO_SEVERITY[aState] - REPO_SEVERITY[bState] || (a.name || a.path).localeCompare(b.name || b.path);
  });
  const rollup = repos.reduce((result, entry) => {
    const [state] = repoCardState(entry);
    if (["error", "warning"].includes(state)) result.attention += 1;
    else if (state === "active") result.running += 1;
    else result.quiet += 1;
    return result;
  }, { attention: 0, running: 0, quiet: 0 });
  return (
    <main>
      <section className="hub-rollup" aria-label="Hub status summary">
        <strong>{rollup.attention} need attention</strong><span>{rollup.running} running</span><span>{rollup.quiet} ready or idle</span>
      </section>
      <section className="hub-grid" aria-label="Registered repos">
        {repos.map((entry) => (
          <RepoCard entry={entry} key={entry.path} onSelect={onSelect} now={now} />
        ))}
      </section>
    </main>
  );
}

function readRepoHash() {
  const match = window.location.hash.match(/^#repo=(.+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function useSnapshotFeed() {
  const [snapshot, setSnapshot] = useState(null);
  const [connection, setConnection] = useState("connecting");

  useEffect(() => {
    let active = true;
    let polling = null;
    let staleTimer = null;
    let lastLiveAt = 0;
    const update = (payload) => {
      if (active && payload?.ok) setSnapshot(payload);
    };
    const fetchSnapshot = async () => {
      try {
        const response = await fetch("/api/snapshot", { cache: "no-store" });
        update(await response.json());
      } catch {
        if (active) setConnection("offline");
      }
    };
    const stopPolling = () => {
      if (polling) window.clearInterval(polling);
      polling = null;
    };
    const markLive = () => {
      lastLiveAt = Date.now();
      if (staleTimer) window.clearTimeout(staleTimer);
      staleTimer = null;
      stopPolling();
      if (active) setConnection("live");
    };
    const startPolling = () => {
      if (!active) return;
      setConnection("polling");
      if (!polling) polling = window.setInterval(fetchSnapshot, 2000);
    };
    fetchSnapshot();
    const source = new EventSource("/api/events");
    source.onopen = markLive;
    source.addEventListener("snapshot", (event) => {
      update(JSON.parse(event.data));
      markLive();
    });
    source.onerror = () => {
      if (!active) return;
      const delay = reconnectDelay(lastLiveAt);
      if (delay > 0) {
        if (staleTimer) window.clearTimeout(staleTimer);
        staleTimer = window.setTimeout(startPolling, delay);
        return;
      }
      startPolling();
    };
    return () => {
      active = false;
      source.close();
      stopPolling();
      if (staleTimer) window.clearTimeout(staleTimer);
    };
  }, []);

  return [snapshot, connection];
}

function initialTheme() {
  const stored = window.localStorage.getItem("mergetrain-theme");
  if (["light", "dark"].includes(stored)) return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function App() {
  const [snapshot, connection] = useSnapshotFeed();
  const [now, setNow] = useState(new Date());
  const [selectedRepo, setSelectedRepo] = useState(readRepoHash);
  const [theme, setTheme] = useState(initialTheme);
  const [demoStep, setDemoStep] = useState(6);
  const [demoPlaying, setDemoPlaying] = useState(false);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  useEffect(() => {
    const onHash = () => setSelectedRepo(readRepoHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("mergetrain-theme", theme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0d1117" : "#fbfaf7");
  }, [theme]);

  useEffect(() => {
    if (!demoPlaying) return undefined;
    if (demoStep >= 6) {
      const stop = window.setTimeout(() => setDemoPlaying(false), 900);
      return () => window.clearTimeout(stop);
    }
    const advance = window.setTimeout(() => setDemoStep((value) => value + 1), 1050);
    return () => window.clearTimeout(advance);
  }, [demoPlaying, demoStep]);

  useEffect(() => {
    if (!snapshot) return;
    if (snapshot.hub) {
      const attention = snapshot.repos.filter((entry) => {
        const [state] = repoCardState(entry);
        return ["error", "warning"].includes(state);
      }).length;
      document.title = `${attention ? `(${attention}) ` : ""}mergetrain · hub`;
      return;
    }
    const failures = (snapshot.counts?.blocked || 0) + (snapshot.counts?.failed || 0) + (snapshot.counts?.needs_reconcile || 0);
    const state = failures ? "attention" : snapshot.train.selection === "running" ? "running" : snapshot.train.selection === "validated" ? "ready" : "idle";
    document.title = `${failures ? `(${failures}) ` : ""}mergetrain · ${state}`;
  }, [snapshot]);

  const selectRepo = (path) => {
    window.location.hash = path === null ? "" : `repo=${encodeURIComponent(path)}`;
    setSelectedRepo(path);
  };
  const playDemo = () => {
    setDemoStep(0);
    setDemoPlaying(true);
  };

  if (!snapshot) return <Loading />;

  if (snapshot.hub) {
    const entry = selectedRepo === null
      ? null
      : snapshot.repos.find((item) => item.path === selectedRepo) || null;
    const drillable = entry?.ok && !entry.empty ? entry : null;
    return (
      <div className="app-shell">
        <Header
          snapshot={snapshot}
          connection={connection}
          now={now}
          hub
          repoName={drillable ? drillable.name || drillable.path : null}
          theme={theme}
          onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
        />
        {snapshot.registry_error && <RegistryErrorBanner message={snapshot.registry_error} />}
        {drillable ? (
          <>
            <button className="hub-back" type="button" onClick={() => selectRepo(null)}>← All repos</button>
            <SingleRepoBody snapshot={drillable.snapshot} now={now} />
          </>
        ) : (
          <HubOverview snapshot={snapshot} onSelect={selectRepo} now={now} />
        )}
        <footer className="page-footer"><WifiHigh size={18} /><span>Read-only local view</span><i>·</i><span>All actions are performed by mergetrain.</span></footer>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Header
        snapshot={snapshot}
        connection={connection}
        now={now}
        theme={theme}
        onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
        demoState={{ playing: demoPlaying, step: demoStep }}
        onPlayDemo={playDemo}
      />
      <SingleRepoBody
        snapshot={snapshot}
        now={now}
        demoStep={snapshot.project.preview ? demoStep : null}
      />
      <footer className="page-footer"><WifiHigh size={18} /><span>Read-only local view</span><i>·</i><span>All actions are performed by mergetrain.</span></footer>
    </div>
  );
}
