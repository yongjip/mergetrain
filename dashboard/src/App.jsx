import { useEffect, useMemo, useState } from "react";
import {
  Broadcast,
  CalendarBlank,
  CheckCircle,
  Circle,
  Clock,
  FileCode,
  GitBranch,
  Heartbeat,
  HourglassHigh,
  Info,
  ListChecks,
  Pulse,
  ShieldCheck,
  SpinnerGap,
  StackSimple,
  TerminalWindow,
  Timer,
  WarningCircle,
  WifiHigh,
  XCircle,
} from "@phosphor-icons/react";

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

function actionCopy(value, words) {
  const actions = {
    wait_for_runner: ["Wait for the current phase to finish.", "The runner will continue automatically."],
    fix_blocked_job: ["Fix the blocked branch and enqueue again.", "Commit a clean result in the owning branch first."],
    deploy_validated_train_when_approved: [`Approve the exact validated train to ${words.action}.`, `Git ${words.noun} remains an explicit CLI action.`],
    cancel_and_reenqueue_legacy_validated_jobs: ["Re-enqueue the legacy validated jobs.", `A fresh train identity is required before ${words.noun}.`],
    run_daemon_or_run_batch_deploy_when_approved: [`Start the approved ${words.action} runner.`, "Only auto-approved jobs are eligible for the daemon."],
    run_batch_validate: ["Start a validation run when ready.", "Nothing will be pushed in validate-only mode."],
    gc_available: ["Clean up completed worktrees.", "Review the dry run before applying cleanup."],
    enqueue_clean_branch: ["Enqueue a committed task branch.", "The queue is ready for the next clean job."],
  };
  return actions[value] || actions.enqueue_clean_branch;
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

function Header({ snapshot, connection, now }) {
  const generated = relative(snapshot.generated_at, now);
  const connectionLabel = connection === "live" ? "CONNECTED" : connection === "offline" ? "DISCONNECTED" : "POLLING";
  return (
    <header className="topbar">
      <div className="brand"><StackSimple size={34} weight="bold" /><strong>mergetrain</strong></div>
      <div className="context"><FileCode size={19} /><span>{snapshot.project.name}</span></div>
      <div className="context"><GitBranch size={19} /><span>{snapshot.project.integration_ref}</span></div>
      <span className="local-badge">LOCAL</span>
      {snapshot.project.preview && <span className="preview-badge">PREVIEW</span>}
      <div className="topbar-spacer" />
      <div className={`live ${connection}`}><span className="live-dot" />{connectionLabel}<small>· updated {generated}</small></div>
      <div className="context divider"><Clock size={19} /><span>{clockTime(now)}</span></div>
      <div className="context divider"><CalendarBlank size={19} /><span>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(now)}</span></div>
    </header>
  );
}

function PreviewBanner() {
  return (
    <div className="preview-banner" role="status">
      <Info size={18} weight="fill" />
      <strong>Preview data</strong>
      <span>Synthetic runner events for UI review. No gate command shown here is actually executing.</span>
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
  const job = jobs.find((item) => (
    item.status === "blocked"
    || item.status === "failed"
    || (item.status === "deployed" && item.verify_status === "failed")
  ));
  const verifyWarning = job?.status === "deployed" && job.verify_status === "failed";
  return (
    <section className="rail-section blocked-section">
      <h2>Attention <small>(history)</small></h2>
      {job ? (
        <div className="blocked-item">
          <div className={`blocked-title ${verifyWarning ? "warning" : "error"}`}>
            {verifyWarning
              ? <WarningCircle size={24} weight="fill" />
              : <XCircle size={24} weight="fill" />}
            <strong>#{job.id}</strong><span>{job.task}</span>
          </div>
          <div className="blocked-detail"><small>Reason</small><p>{job.note || "No reason recorded"}</p><small>Occurred</small><code>{dateTime(job.finished_at || job.requested_at)}</code></div>
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

export function App() {
  const [snapshot, setSnapshot] = useState(null);
  const [connection, setConnection] = useState("connecting");
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const tick = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  useEffect(() => {
    let active = true;
    let polling = null;
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
    fetchSnapshot();
    const source = new EventSource("/api/events");
    source.addEventListener("snapshot", (event) => {
      update(JSON.parse(event.data));
      setConnection("live");
      if (polling) {
        window.clearInterval(polling);
        polling = null;
      }
    });
    source.onerror = () => {
      if (!active) return;
      setConnection("polling");
      if (!polling) polling = window.setInterval(fetchSnapshot, 2000);
    };
    return () => {
      active = false;
      source.close();
      if (polling) window.clearInterval(polling);
    };
  }, []);

  const recentJobs = useMemo(() => snapshot?.jobs || [], [snapshot]);
  if (!snapshot) return <Loading />;
  return (
    <div className="app-shell">
      <Header snapshot={snapshot} connection={connection} now={now} />
      {snapshot.project.preview && <PreviewBanner />}
      <div className="dashboard-grid">
        <main className="main-column">
          <Hero snapshot={snapshot} now={now} />
          <PhaseRail snapshot={snapshot} />
          <CurrentWork snapshot={snapshot} now={now} />
          <JobCards snapshot={snapshot} />
          <Activity events={snapshot.events} jobCount={snapshot.train.jobs.length} words={terminology(snapshot)} />
        </main>
        <aside className="side-rail">
          <RunnerPanel snapshot={snapshot} now={now} />
          <AttentionPanel jobs={recentJobs} />
          <NextAction snapshot={snapshot} />
        </aside>
      </div>
      <footer className="page-footer"><WifiHigh size={18} /><span>Read-only local view</span><i>·</i><span>All actions are performed by mergetrain.</span></footer>
    </div>
  );
}
