import { CaretDown } from "@phosphor-icons/react";

import { contextualInspectorState, currentTrainModel, workspaceStepForSnapshot } from "../../dashboardLogic.js";
import { DEPLOY_WORDS } from "../../dashboardFormatters.js";
import { Activity, AttentionPanel, DeploymentHistory, RunnerPanel } from "../OperationalDetails.jsx";
import { BatchStatusBanner, MobileGlance } from "./BatchStatus.jsx";
import { CurrentTrainWorkspace } from "./CurrentTrainWorkspace.jsx";
import { NextBatchPanel } from "./NextBatchPanel.jsx";
import { TrainInspector } from "./TrainInspector.jsx";

export function SingleRepoBody({ snapshot, now, demoStep }) {
  const recentJobs = snapshot.jobs || [];
  const words = DEPLOY_WORDS;
  const model = currentTrainModel(snapshot);
  const inspector = contextualInspectorState(snapshot, demoStep, model);
  const showInspector = inspector.blockedJobs.length > 0;
  const step = demoStep ?? workspaceStepForSnapshot(snapshot);
  return (
    <main className="workspace-shell">
      <MobileGlance snapshot={snapshot} model={model} now={now} />
      <BatchStatusBanner snapshot={snapshot} model={model} now={now} />
      {!!model.currentJobs.length && (
        <div className={`train-workspace-grid ${showInspector ? "with-inspector" : ""}`}>
          <CurrentTrainWorkspace snapshot={snapshot} demoStep={demoStep} model={model} now={now} />
          {showInspector && <TrainInspector inspector={inspector} />}
        </div>
      )}
      <NextBatchPanel jobs={model.nextBatchJobs} />
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
