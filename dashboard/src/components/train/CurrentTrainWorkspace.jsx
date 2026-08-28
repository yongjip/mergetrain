import { Clock, ListChecks } from "@phosphor-icons/react";

import {
  blockedReason,
  currentTrainModel,
  gateWaterfallModel,
  jobLabel,
  newestFirstFifoRows,
  workspaceStepForSnapshot,
} from "../../dashboardLogic.js";
import { duration, parseTime, relative, shortSha } from "../../dashboardFormatters.js";
import { StatusIcon } from "../StatusIcon.jsx";

const WORKSPACE_PHASES = [
  ["queue", "Queued"],
  ["merge", "Merged"],
  ["gate", "Tests passed"],
  ["ready", "Approval"],
];

function workspacePhaseState(index, step) {
  if (index === 0) return step === 0 ? "active" : "done";
  if (index === 1) return step < 1 ? "waiting" : step <= 4 ? "active" : "done";
  if (index === 2) return step < 5 ? "waiting" : step === 5 ? "active" : "done";
  return step < 6 ? "waiting" : "active";
}

export function WorkspacePhaseRail({ step, approval }) {
  return (
    <ol className={`workspace-phase-rail ${approval ? "approval" : ""}`} aria-label="Batch lifecycle">
      {WORKSPACE_PHASES.map(([key, label], index) => {
        const state = workspacePhaseState(index, step);
        return (
          <li className={state} key={key}>
            <span>{label}</span>
            <i>
              {approval && key === "ready" && state === "active"
                ? <Clock size={23} weight="fill" />
                : <StatusIcon state={state} size={23} />}
            </i>
          </li>
        );
      })}
    </ol>
  );
}

export function TrainJobRow({ job, blocked, order, turn, step }) {
  const mergeReached = step >= turn;
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
  const approvalState = blocked && mergeReached
    ? "error"
    : gatePassed
      ? "approval"
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
          <code>{shortSha(job.head_sha || job.validated_head_sha)}</code>
        </div>
      </div>
      <div className="job-cell branch-cell" role="cell">
        <code>{job.branch || "branch pending"}</code>
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
              ? "Tests passed"
              : gateRunning
                ? "Tests running"
                : "Waiting"}
        </span>
      </div>
      <div className={`job-cell outcome-cell ${approvalState}`} role="cell">
        {approvalState === "approval"
          ? <Clock size={17} weight="fill" />
          : <StatusIcon state={approvalState} size={17} />}
        <span>
          {blocked && mergeReached
            ? "Rebase"
            : gatePassed
              ? "Awaiting approval"
              : mergeReached
                ? "Candidate"
                : "Queued"}
        </span>
      </div>
    </div>
  );
}

export function FifoJobList({ jobs, blockedIds, step }) {
  if (!jobs.length) return null;
  const newestFirstRows = newestFirstFifoRows(jobs);
  const fifoJobs = [...newestFirstRows].reverse().map(({ job }) => job);
  const blockedCount = jobs.filter((job) => blockedIds.has(String(job.id))).length;
  return (
    <section className={`train-job-group fifo ${step >= 6 ? "resolved" : "pending"}`}>
      <header>
        <div>
          <ListChecks size={19} weight="fill" />
          <strong>{step >= 6 ? "Exact train" : "FIFO merge order"}</strong>
          <span>{fifoJobs.map((job) => `#${job.id}`).join(" → ")}</span>
        </div>
        <span>
          {step >= 6
            ? `FIFO order · ${blockedCount} skipped`
            : step >= 1
              ? "Newest first · merging one by one"
              : "Newest first · FIFO runs oldest first"}
        </span>
      </header>
      <div role="rowgroup">
        {newestFirstRows.map(({ job, order }) => (
          <TrainJobRow
            job={job}
            blocked={blockedIds.has(String(job.id))}
            order={order}
            turn={order}
            step={step}
            key={job.id}
          />
        ))}
      </div>
    </section>
  );
}

export function GateWaterfall({ snapshot, now }) {
  const gates = gateWaterfallModel(snapshot.eta);
  if (!gates.some((gate) => gate.sample_count)) return null;
  return (
    <section className="gate-waterfall" aria-labelledby="gate-waterfall-title">
      <header>
        <div>
          <span className="workspace-eyebrow">Recent local history</span>
          <h2 id="gate-waterfall-title">Gate timing</h2>
        </div>
        <span>Median of up to {snapshot.eta.sample_limit} completed runs</span>
      </header>
      <ol>
        {gates.map((gate) => {
          const liveStartedAt = snapshot.progress?.gates?.find(
            (item) => item.index === gate.index,
          )?.started_at;
          const liveStarted = parseTime(liveStartedAt);
          const liveElapsed = gate.state === "active" && liveStarted
            ? Math.max(0, (now.getTime() - liveStarted) / 1000)
            : gate.elapsed_seconds;
          const progressPercent = gate.median_seconds && liveElapsed !== null
            ? Math.min(100, Math.round((liveElapsed / gate.median_seconds) * 100))
            : gate.state === "success"
              ? 100
              : 0;
          return (
            <li className={gate.state} key={`${gate.index}-${gate.name}`}>
              <div>
                <span>{gate.index}</span>
                <strong>{gate.name}</strong>
                <small>
                  {gate.state === "active" && liveElapsed !== null
                    ? `${duration(liveElapsed)} elapsed`
                    : gate.median_seconds !== null
                      ? `${duration(gate.median_seconds)} median`
                      : "building history"}
                </small>
              </div>
              <span
                className="gate-duration-track"
                style={{
                  "--gate-width": `${gate.widthPercent}%`,
                  "--gate-progress": `${progressPercent}%`,
                }}
              >
                <i />
              </span>
              <em>{gate.sample_count} run{gate.sample_count === 1 ? "" : "s"}</em>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function CurrentTrainWorkspace({
  snapshot,
  demoStep,
  now,
  model = currentTrainModel(snapshot),
}) {
  const { blockedJobs, currentJobs, validatedTrain, selection } = model;
  const step = demoStep ?? workspaceStepForSnapshot(snapshot);
  const blockedIds = new Set(blockedJobs.map((job) => String(job.id)));

  return (
    <section className="current-train-card" aria-label="Current batch details">
      <WorkspacePhaseRail step={step} approval={selection === "validated"} />
      <GateWaterfall snapshot={snapshot} now={now} />

      <div className="train-table" role="table" aria-label="Current batch FIFO requests and outcomes">
        <div className="train-table-head" role="row">
          <span role="columnheader">Order</span>
          <span role="columnheader">Merge request</span>
          <span role="columnheader">Branch</span>
          <span role="columnheader">Merged</span>
          <span role="columnheader">Tests</span>
          <span role="columnheader">Approval</span>
        </div>
        <FifoJobList jobs={currentJobs} blockedIds={blockedIds} step={step} />
      </div>

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
