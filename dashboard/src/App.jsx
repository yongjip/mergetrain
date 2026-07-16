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
  Pulse,
  ShieldCheck,
  SpinnerGap,
  StackSimple,
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

const ACTIONS = {
  wait_for_runner: ["Wait for the current phase to finish.", "The runner will continue automatically."],
  fix_blocked_job: ["Fix the blocked branch and enqueue again.", "Commit a clean result in the owning branch first."],
  deploy_validated_train_when_approved: ["Approve the exact validated train.", "Deployment remains an explicit CLI action."],
  cancel_and_reenqueue_legacy_validated_jobs: ["Re-enqueue the legacy validated jobs.", "A fresh train identity is required before deploy."],
  run_daemon_or_run_batch_deploy_when_approved: ["Start the approved deploy runner.", "Only auto-approved jobs are eligible for the daemon."],
  run_batch_validate: ["Start a validation run when ready.", "Nothing will be pushed in validate-only mode."],
  gc_available: ["Clean up completed worktrees.", "Review the dry run before applying cleanup."],
  enqueue_clean_branch: ["Enqueue a committed task branch.", "The queue is ready for the next clean job."],
};

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
  if (progress.completed_phases?.includes(key)) return "done";
  if (index < current) return "done";
  if (index === current) return progress.state === "queued" ? "waiting" : "active";
  return "waiting";
}

function StatusIcon({ state, size = 22 }) {
  if (state === "done" || state === "success") return <CheckCircle size={size} weight="fill" />;
  if (state === "active") return <SpinnerGap size={size} weight="bold" className="spin" />;
  if (state === "error") return <XCircle size={size} weight="fill" />;
  if (state === "warning") return <WarningCircle size={size} weight="fill" />;
  return <Circle size={size} weight="regular" />;
}

function Header({ snapshot, connection, now }) {
  const generated = relative(snapshot.generated_at, now);
  const connectionLabel = connection === "live" ? "LIVE" : connection === "offline" ? "OFFLINE" : "POLLING";
  return (
    <header className="topbar">
      <div className="brand"><StackSimple size={34} weight="bold" /><strong>mergetrain</strong></div>
      <div className="context"><FileCode size={19} /><span>{snapshot.project.name}</span></div>
      <div className="context"><GitBranch size={19} /><span>{snapshot.project.integration_ref}</span></div>
      <span className="local-badge">LOCAL</span>
      <div className="topbar-spacer" />
      <div className={`live ${connection}`}><span className="live-dot" />{connectionLabel}<small>· updated {generated}</small></div>
      <div className="context divider"><Clock size={19} /><span>{clockTime(now)}</span></div>
      <div className="context divider"><CalendarBlank size={19} /><span>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(now)}</span></div>
    </header>
  );
}

function Hero({ snapshot, now }) {
  const jobs = snapshot.train.jobs;
  const selection = snapshot.train.selection;
  const deploying = jobs.some((job) => job.status === "in_progress" && job.train_id);
  const title = selection === "running"
    ? `${deploying ? "Deploying" : "Validating"} train · ${jobs.length} job${jobs.length === 1 ? "" : "s"}`
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
        const state = ["validated", "deployed"].includes(job.status) ? "done" : ["blocked", "failed"].includes(job.status) ? "error" : active ? "active" : assembled ? "done" : "waiting";
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

function Activity({ events }) {
  const hasTrainAssembly = events.some((event) => event.phase === "assembling" && event.job_id === null);
  const visible = events
    .filter((event) => !(hasTrainAssembly && event.phase === "assembling" && event.job_id !== null))
    .slice(-5)
    .reverse();
  return (
    <section className="activity">
      <h2>Activity</h2>
      {!visible.length && <p className="empty-copy">Runner events will appear here as the train moves.</p>}
      <div className="activity-list">
        {visible.map((event) => (
          <article className={`activity-row ${event.state}`} key={event.id}>
            <time>{clockTime(event.created_at)}</time>
            <span className="event-icon"><StatusIcon state={event.state === "success" ? "done" : event.state} size={19} /></span>
            <div><strong>{event.message}</strong>{event.detail && <small>{event.detail}</small>}</div>
          </article>
        ))}
      </div>
    </section>
  );
}

function RunnerPanel({ snapshot, now }) {
  const lock = snapshot.lock;
  const alive = lock?.liveness === "alive";
  return (
    <section className="rail-section runner-section">
      <div className="rail-heading"><h2>Runner</h2><span className={alive ? "healthy" : "muted"}><i />{lock?.owner || "idle"}</span></div>
      <dl>
        <div><dt><Heartbeat size={22} />Health</dt><dd className={alive ? "healthy" : "muted"}>{alive ? "Healthy" : "Idle"}</dd></div>
        <div><dt><Pulse size={22} />Heartbeat</dt><dd className={alive ? "healthy" : "muted"}>{lock ? relative(lock.heartbeat_at, now) : "—"}</dd></div>
        <div><dt><Timer size={22} />Lease expires</dt><dd className="attention">{lock ? relative(lock.expires_at, now) : "—"}</dd></div>
      </dl>
    </section>
  );
}

function BlockedPanel({ jobs }) {
  const job = jobs.find((item) => item.status === "blocked" || item.status === "failed");
  return (
    <section className="rail-section blocked-section">
      <h2>Blocked <small>(history)</small></h2>
      {job ? (
        <div className="blocked-item">
          <div className="blocked-title"><XCircle size={24} weight="fill" /><strong>#{job.id}</strong><span>{job.task}</span></div>
          <div className="blocked-detail"><small>Reason</small><p>{job.note || "No reason recorded"}</p><small>Occurred</small><code>{dateTime(job.finished_at || job.requested_at)}</code></div>
        </div>
      ) : (
        <div className="clear-history"><CheckCircle size={24} weight="fill" /><span>No blocked jobs in recent history.</span></div>
      )}
    </section>
  );
}

function NextAction({ value }) {
  const [title, detail] = ACTIONS[value] || ACTIONS.enqueue_clean_branch;
  return (
    <section className="rail-section action-section">
      <h2>Next safe action</h2>
      <div className="action-title"><HourglassHigh size={29} weight="duotone" /><strong>{title}</strong></div>
      <p>{detail}</p>
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
      <div className="dashboard-grid">
        <main className="main-column">
          <Hero snapshot={snapshot} now={now} />
          <PhaseRail snapshot={snapshot} />
          <JobCards snapshot={snapshot} />
          <Activity events={snapshot.events} />
        </main>
        <aside className="side-rail">
          <RunnerPanel snapshot={snapshot} now={now} />
          <BlockedPanel jobs={recentJobs} />
          <NextAction value={snapshot.next_action} />
        </aside>
      </div>
      <footer className="page-footer"><WifiHigh size={18} /><span>Read-only local view</span><i>·</i><span>All actions are performed by mergetrain.</span></footer>
    </div>
  );
}
