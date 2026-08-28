import { HourglassHigh, ListChecks } from "@phosphor-icons/react";

import { jobLabel, newestFirstFifoRows } from "../../dashboardLogic.js";

export function NextBatchPanel({ jobs }) {
  if (!jobs.length) return null;
  const rows = newestFirstFifoRows(jobs);
  const fifoNames = [...rows].reverse().map(({ job }) => `#${job.id}`).join(" → ");
  return (
    <section className="next-batch-card" aria-labelledby="next-batch-title">
      <header>
        <div>
          <span className="workspace-eyebrow">Arrived after batch lock</span>
          <h2 id="next-batch-title">Next batch · {jobs.length} waiting</h2>
          <p>These requests stay queued until the current batch finishes.</p>
        </div>
        <span className="next-batch-status"><HourglassHigh size={17} />Not in current batch</span>
      </header>
      <div className="next-batch-list">
        {rows.slice(0, 4).map(({ job }) => (
          <article key={job.id}>
            <strong>#{job.id}</strong>
            <div>
              <span>{jobLabel(job)}</span>
              <code>{job.branch || "branch pending"}</code>
            </div>
            <small>Waiting</small>
          </article>
        ))}
        {rows.length > 4 && <span className="next-batch-more">+{rows.length - 4} more queued</span>}
      </div>
      <footer><ListChecks size={16} />FIFO order <code>{fifoNames}</code></footer>
    </section>
  );
}
