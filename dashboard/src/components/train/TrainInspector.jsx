import { XCircle } from "@phosphor-icons/react";

import { blockedReason, conflictFiles, jobLabel } from "../../dashboardLogic.js";

export function NeedsAttentionPanel({ jobs }) {
  return (
    <section className="inspector-section context-panel attention-panel">
      <div className="context-panel-heading">
        <span className="context-eyebrow"><XCircle size={17} weight="fill" />Needs attention</span>
        <strong>{jobs.length} blocked</strong>
      </div>
      {jobs.map((job) => {
        const files = conflictFiles(job);
        return (
          <article className="context-job" key={job.id}>
            <div className="context-job-title">
              <strong>#{job.id} · {jobLabel(job)}</strong>
              <span><XCircle size={14} weight="fill" />{blockedReason(job)}</span>
            </div>
            <code>{job.branch || "branch pending"}</code>
            {!!files.length && (
              <div className="conflict-files" aria-label={`Conflicting files for job ${job.id}`}>
                {files.map((file) => <code key={file}>{file}</code>)}
              </div>
            )}
            <p>Rebase on latest main, resolve the conflict, commit, then enqueue a fresh request.</p>
          </article>
        );
      })}
    </section>
  );
}

export function TrainInspector({ inspector }) {
  if (!inspector.blockedJobs.length) return null;
  return (
    <aside className="train-inspector" aria-label="Items that need operator judgment">
      {!!inspector.blockedJobs.length && (
        <NeedsAttentionPanel jobs={inspector.blockedJobs} />
      )}
    </aside>
  );
}
