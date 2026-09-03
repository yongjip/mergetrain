import { useState } from "react";
import { ArrowRight, GitBranch, Heartbeat, MagnifyingGlass, StackSimple } from "@phosphor-icons/react";

import {
  attentionCount,
  currentTrainModel,
  historyState,
  jobActivityAt,
  jobLabel,
  latestRepoJob,
  repoOperationalCopy,
  repoStateForEntry,
} from "../../dashboardLogic.js";
import { DEPLOY_WORDS, relative } from "../../dashboardFormatters.js";

export function RepoHistory({ jobs = [] }) {
  const recent = [...jobs]
    .sort((a, b) => Date.parse(jobActivityAt(a)) - Date.parse(jobActivityAt(b)))
    .slice(-14);
  if (!recent.length) return <span className="repo-history-empty">No history</span>;
  const summary = recent
    .map((job) => `${job.id}: ${[job.status, job.verify_status].filter(Boolean).join(":")}`)
    .join(", ");
  return (
    <div className="repo-history" role="img" aria-label={`Recent train outcomes: ${summary}`}>
      {recent.map((job, index) => (
        <span
          aria-hidden="true"
          className={`repo-history-mark ${historyState(job)}`}
          key={`${job.id}-${index}`}
          style={{ "--history-height": `${14 + ((Number(job.id) + index * 3) % 13)}px` }}
        />
      ))}
    </div>
  );
}

export function RepoTableRow({ entry, onSelect, now }) {
  const [state, label] = repoStateForEntry(entry);
  const name = entry.name || entry.path;
  const snapshot = entry.ok && !entry.empty ? entry.snapshot : null;
  const batch = snapshot ? currentTrainModel(snapshot) : null;
  const words = DEPLOY_WORDS;
  const status = repoOperationalCopy(entry, snapshot, state, words);
  const currentJob = batch?.currentJobs.length ? latestRepoJob(batch.currentJobs) : null;
  const latestDeployed = snapshot
    ? latestRepoJob(snapshot.jobs?.filter((job) => job.status === "deployed"))
    : null;
  const featuredJob = currentJob || latestDeployed || latestRepoJob(snapshot?.jobs);
  const featuredLabel = currentJob
    ? batch.selection === "validated" ? "Validated train" : "Current batch"
    : latestDeployed ? "Latest deployment" : "Latest activity";
  const activity = featuredJob ? relative(jobActivityAt(featuredJob), now) : "—";
  const attention = snapshot ? attentionCount(snapshot.counts) : 0;
  const queueSummary = batch?.currentJobs.length
    ? <>Current <strong>{batch.currentJobs.length}</strong><i>·</i> Next <strong>{batch.nextBatchJobs.length}</strong></>
    : <>Queued <strong>{snapshot?.counts?.queued || 0}</strong><i>·</i> Attention <strong>{attention}</strong></>;
  const runnerLabel = snapshot?.lock ? "Runner active" : "Runner idle";
  const clickable = Boolean(snapshot);
  const content = (
    <>
      <div className="repo-table-identity">
        <strong>{name}</strong>
        <code>{entry.path}</code>
        <span><GitBranch size={15} />{snapshot?.project?.integration_ref || "integration unavailable"}</span>
      </div>
      <div className="repo-table-work">
        {featuredJob ? (
          <>
            <span className={`repo-work-kind ${state}`}>
              {featuredLabel} #{featuredJob.id}
            </span>
            <strong>{jobLabel(featuredJob)}</strong>
            <time>{activity}</time>
          </>
        ) : (
          <>
            <span className={`repo-work-kind ${state}`}>{status.title}</span>
            <strong>{status.detail}</strong>
          </>
        )}
      </div>
      <div className="repo-table-queue">
        <span>{queueSummary}</span>
        <time>{activity}</time>
      </div>
      <div className="repo-table-activity">
        <RepoHistory jobs={snapshot?.jobs} />
      </div>
      <div className="repo-table-runner">
        <Heartbeat size={20} />
        <span>{runnerLabel}</span>
      </div>
      <div className="repo-table-state">
        <span className={`state-pill ${state}`}>{label}</span>
        {state !== "approval" && <span className={`repo-state-detail ${state}`}>{status.title}</span>}
        {clickable && <span className="repo-open">Open details <ArrowRight size={15} /></span>}
      </div>
      <ArrowRight aria-hidden="true" className="repo-row-arrow" size={22} />
    </>
  );
  if (!clickable) {
    return <article className={`repo-table-row ${state}`}>{content}</article>;
  }
  return (
    <button
      aria-label={`Open ${name} details. ${status.title}.`}
      className={`repo-table-row ${state} clickable`}
      onClick={() => onSelect(entry.path)}
      type="button"
    >
      {content}
    </button>
  );
}

const REPO_SEVERITY = {
  error: 0,
  warning: 1,
  approval: 2,
  active: 3,
  queued: 4,
  waiting: 5,
  idle: 6,
};

export function HubOverview({ snapshot, onSelect, now }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
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
    const [aState] = repoStateForEntry(a);
    const [bState] = repoStateForEntry(b);
    return REPO_SEVERITY[aState] - REPO_SEVERITY[bState] || (a.name || a.path).localeCompare(b.name || b.path);
  });
  const rollup = repos.reduce((result, entry) => {
    const [state] = repoStateForEntry(entry);
    if (["error", "warning"].includes(state)) result.attention += 1;
    else if (state === "active") result.running += 1;
    else if (state === "approval") result.approval += 1;
    else if (state === "queued") result.queued += 1;
    else result.clear += 1;
    return result;
  }, { attention: 0, running: 0, approval: 0, queued: 0, clear: 0 });
  const normalizedQuery = query.trim().toLowerCase();
  const visibleRepos = repos.filter((entry) => {
    const [state] = repoStateForEntry(entry);
    const snapshot = entry.ok && !entry.empty ? entry.snapshot : null;
    const featured = latestRepoJob(snapshot?.jobs);
    const haystack = [entry.name, entry.path, featured && jobLabel(featured)]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    const matchesQuery = !normalizedQuery || haystack.includes(normalizedQuery);
    const matchesFilter = filter === "all"
      || (filter === "action" && ["error", "warning", "approval"].includes(state))
      || (filter === "running" && ["active", "queued"].includes(state))
      || (filter === "clear" && ["idle", "waiting"].includes(state));
    return matchesQuery && matchesFilter;
  });
  return (
    <main>
      <section className="hub-rollup" aria-label="Hub status summary">
        <div className="hub-rollup-metrics">
          <strong>{repos.length} repositories</strong>
          <span className="approval">{rollup.approval} awaiting approval</span>
          <span className="running">{rollup.running} running</span>
          <span className="attention">{rollup.attention} need attention</span>
          <span className="clear">{rollup.clear} queue clear</span>
          {!!rollup.queued && <span className="queued">{rollup.queued} queued</span>}
        </div>
        <div className="hub-toolbar">
          <label className="hub-search">
            <MagnifyingGlass aria-hidden="true" size={18} />
            <span className="sr-only">Filter repositories</span>
            <input
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter repositories"
              type="search"
              value={query}
            />
          </label>
          <select aria-label="Filter repositories by status" onChange={(event) => setFilter(event.target.value)} value={filter}>
            <option value="all">All</option>
            <option value="action">Needs action</option>
            <option value="running">Running or queued</option>
            <option value="clear">Queue clear</option>
          </select>
        </div>
      </section>
      <section className="hub-table" aria-label="Registered repos">
        <header className="repo-table-head" aria-hidden="true">
          <span>Repository</span>
          <span>Current train</span>
          <span>Queue</span>
          <span>Recent activity</span>
          <span>Runner</span>
          <span>State</span>
          <span />
        </header>
        <div className="repo-table-body">
          {visibleRepos.map((entry) => (
            <RepoTableRow entry={entry} key={entry.path} onSelect={onSelect} now={now} />
          ))}
          {!visibleRepos.length && (
            <div className="repo-table-empty">
              <MagnifyingGlass size={22} />
              <strong>No repositories match this view.</strong>
              <span>Adjust the search or status filter.</span>
            </div>
          )}
        </div>
      </section>
    </main>
  );
}
