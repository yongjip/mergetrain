import {
  Circle,
  Clock,
  HourglassHigh,
  SpinnerGap,
  WarningCircle,
} from "@phosphor-icons/react";

import {
  actionCopy,
  browserIndicator,
  etaRemainingSeconds,
  jobLabel,
} from "../../dashboardLogic.js";
import { DEPLOY_WORDS, duration, relative } from "../../dashboardFormatters.js";

function hasOnlyVerificationAttention(jobs = []) {
  return jobs.length > 0 && jobs.every((job) => (
    job.status === "deployed" && ["failed", "unknown"].includes(job.verify_status)
  ));
}

export function BatchStatusBanner({ snapshot, model, now }) {
  const { currentJobs, selection } = model;
  const attention = model.attentionJobs || [];
  const count = currentJobs.length;
  const words = DEPLOY_WORDS;
  const target = snapshot.project.integration_ref;
  const progress = snapshot.progress || {};
  const currentGate = progress.current_gate
    || progress.gates?.find((gate) => gate.state === "active")
    || null;
  const etaSeconds = etaRemainingSeconds(snapshot.eta, now.getTime());

  let tone = "idle";
  let title = "No active batch";
  let detail = "Queue is clear";
  let runner = "Runner idle";
  let runnerDetail = "Waiting for the next request";
  let nextAction = "Enqueue a committed task branch";
  let icon = <Circle size={25} />;

  if (selection === "idle" && attention.length) {
    const [actionTitle] = actionCopy(snapshot.next_action, words);
    tone = "attention";
    title = hasOnlyVerificationAttention(attention)
      ? "Verification needs attention"
      : "Queue needs attention";
    detail = `${attention.length} request${attention.length === 1 ? "" : "s"} unresolved`;
    runnerDetail = "Waiting for operator review";
    nextAction = actionTitle;
    icon = <WarningCircle size={25} weight="fill" />;
  } else if (selection === "validated") {
    tone = "approval";
    title = "Awaiting deploy approval";
    detail = "Tests passed · Not on main yet";
    runnerDetail = `Last activity ${relative(progress.updated_at || snapshot.generated_at, now)}`;
    nextAction = `Approve ${words.noun} to ${target}`;
    icon = <Clock size={25} weight="fill" />;
  } else if (selection === "running") {
    tone = "running";
    title = progress.phase === "gating" ? "Running tests" : "Running batch";
    detail = currentGate
      ? `Gate ${currentGate.index}/${currentGate.total} · ${currentGate.name}`
      : progress.message || "The runner is processing this batch";
    runner = "Runner active";
    runnerDetail = snapshot.lock
      ? `Heartbeat ${relative(snapshot.lock.heartbeat_at, now)}`
      : "Work is in progress";
    nextAction = "Wait for the current phase to finish";
    icon = <SpinnerGap size={25} className="spin" />;
  } else if (selection === "queued") {
    tone = "queued";
    title = "Queued for validation";
    detail = "Not started yet";
    runnerDetail = `${count} request${count === 1 ? "" : "s"} waiting`;
    nextAction = "Start the runner when ready";
    icon = <HourglassHigh size={25} weight="fill" />;
  }

  return (
    <section className={`batch-status-banner ${tone}`} aria-labelledby="batch-status-title">
      <div className="batch-status-primary">
        <span className="batch-status-icon">{icon}</span>
        <div>
          <h1 id="batch-status-title">{title}</h1>
          <p>{detail}</p>
        </div>
      </div>
      <dl className="batch-status-facts">
        <div>
          <dt>Batch</dt>
          <dd>{count} request{count === 1 ? "" : "s"}</dd>
          <small>in this train</small>
        </div>
        <div>
          <dt>Runner</dt>
          <dd>{runner}</dd>
          <small>{runnerDetail}</small>
        </div>
        <div>
          <dt>Estimated</dt>
          <dd>{etaSeconds === null ? "Building history" : `~${duration(etaSeconds)} left`}</dd>
          <small>
            {snapshot.eta?.sample_count
              ? `${snapshot.eta.sample_count} recent run${snapshot.eta.sample_count === 1 ? "" : "s"} · median`
              : "appears after a completed comparable run"}
          </small>
        </div>
      </dl>
      <div className="batch-next-action">
        <span>Next action</span>
        <strong>{nextAction}</strong>
      </div>
    </section>
  );
}

export function MobileGlance({ snapshot, model, now }) {
  const indicator = browserIndicator(snapshot);
  const [nextTitle] = actionCopy(snapshot.next_action, DEPLOY_WORDS);
  const etaSeconds = etaRemainingSeconds(snapshot.eta, now.getTime());
  const attention = model.attentionJobs || [];
  const stateTitle = attention.length && model.selection === "idle"
    ? hasOnlyVerificationAttention(attention)
      ? "Verification needs attention"
      : "Queue needs attention"
    : model.selection === "validated"
    ? "Awaiting deploy approval"
    : model.selection === "running"
      ? "Runner active"
      : model.selection === "queued"
        ? "Queued for validation"
        : "Queue clear";
  return (
    <section className={`mobile-glance ${indicator.state}`} aria-label="Phone glance status">
      <article>
        <span>State</span>
        <strong>{indicator.glyph} {stateTitle}</strong>
        <small>
          {etaSeconds === null ? snapshot.progress?.message : `About ${duration(etaSeconds)} remaining`}
        </small>
      </article>
      <article>
        <span>Next action</span>
        <strong>{nextTitle}</strong>
      </article>
      <article className={attention.length ? "attention" : "clear"}>
        <span>Attention</span>
        <strong>{attention.length
          ? attention.length === 1 ? "1 request needs review" : `${attention.length} requests need review`
          : "Nothing needs intervention"}</strong>
        {!!attention.length && <small>{attention.slice(0, 3).map((job) => `#${job.id} ${jobLabel(job)}`).join(" · ")}</small>}
      </article>
    </section>
  );
}
