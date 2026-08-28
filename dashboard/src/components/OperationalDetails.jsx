import { useState } from "react";
import {
  CheckCircle,
  GitBranch,
  Heartbeat,
  ListChecks,
  Pulse,
  TerminalWindow,
  Timer,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";

import { clockTime, dateTime, duration, parseTime, relative, shortSha } from "../dashboardFormatters.js";
import { PHASE_LABELS, STATE_LABELS, eventDescription } from "../dashboardPresentation.js";
import { StatusIcon } from "./StatusIcon.jsx";

export function DeploymentHistory({ jobs, words }) {
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

export function Activity({ events, jobCount, words }) {
  const [density, setDensity] = useState("compact");
  const [visibleCount, setVisibleCount] = useState(5);
  const hasTrainAssembly = events.some((event) => event.phase === "assembling" && event.job_id === null);
  const filtered = events
    .filter((event) => !(hasTrainAssembly && event.phase === "assembling" && event.job_id !== null));
  const visible = filtered
    .slice(-visibleCount)
    .reverse();
  const changeDensity = (value) => {
    setDensity(value);
    setVisibleCount(value === "compact" ? 5 : 10);
  };
  return (
    <section className={`activity ${density}`}>
      <div className="activity-heading">
        <div><h2>Activity</h2><span>Newest first · runner milestones</span></div>
        <div className="activity-controls" aria-label="Activity density">
          <button
            className={density === "compact" ? "active" : ""}
            type="button"
            onClick={() => changeDensity("compact")}
          >
            Compact
          </button>
          <button
            className={density === "expanded" ? "active" : ""}
            type="button"
            onClick={() => changeDensity("expanded")}
          >
            Expanded
          </button>
        </div>
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
      {filtered.length > visibleCount && (
        <button
          className="activity-more"
          type="button"
          onClick={() => setVisibleCount((count) => count + 10)}
        >
          Show {Math.min(10, filtered.length - visibleCount)} more
        </button>
      )}
    </section>
  );
}

export function RunnerPanel({ snapshot, now }) {
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

export function AttentionPanel({ jobs }) {
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
