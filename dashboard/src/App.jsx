import { useEffect, useState } from "react";

import { browserIndicator, stateFavicon } from "./dashboardLogic.js";
import { FeedErrorBanner, Header, Loading, PageFooter, RegistryErrorBanner } from "./components/AppChrome.jsx";
import { HubOverview } from "./components/hub/HubOverview.jsx";
import { SingleRepoBody } from "./components/train/SingleRepoBody.jsx";
import { useDashboardNotifications } from "./hooks/useDashboardNotifications.js";
import { readRepoHash, useSnapshotFeed } from "./hooks/useSnapshotFeed.js";

function initialTheme() {
  const stored = window.localStorage.getItem("mergetrain-theme");
  if (["light", "dark"].includes(stored)) return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function App() {
  const feed = useSnapshotFeed();
  const { snapshot, connection, error: feedError, lastSuccessAt } = feed;
  const notifications = useDashboardNotifications(snapshot, feedError);
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
    const indicator = browserIndicator(snapshot);
    document.title = `${indicator.glyph} ${indicator.count ? `(${indicator.count}) ` : ""}mergetrain · ${indicator.label}`;
    const favicon = document.querySelector('link[rel="icon"]');
    if (favicon) favicon.setAttribute("href", stateFavicon(indicator));
  }, [snapshot]);

  const selectRepo = (path) => {
    window.location.hash = path === null ? "" : `repo=${encodeURIComponent(path)}`;
    setSelectedRepo(path);
  };
  const playDemo = () => {
    setDemoStep(0);
    setDemoPlaying(true);
  };

  if (!snapshot) return <Loading error={feedError} />;

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
          notifications={notifications}
          theme={theme}
          onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
        />
        <FeedErrorBanner error={feedError} lastSuccessAt={lastSuccessAt} now={now} />
        {snapshot.registry_error && <RegistryErrorBanner message={snapshot.registry_error} />}
        {drillable ? (
          <>
            <button className="hub-back" type="button" onClick={() => selectRepo(null)}>← All repos</button>
            <SingleRepoBody snapshot={drillable.snapshot} now={now} />
          </>
        ) : (
          <HubOverview snapshot={snapshot} onSelect={selectRepo} now={now} />
        )}
        <PageFooter />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Header
        snapshot={snapshot}
        connection={connection}
        now={now}
        notifications={notifications}
        theme={theme}
        onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
        demoState={{ playing: demoPlaying, step: demoStep }}
        onPlayDemo={playDemo}
      />
      <FeedErrorBanner error={feedError} lastSuccessAt={lastSuccessAt} now={now} />
      <SingleRepoBody
        snapshot={snapshot}
        now={now}
        demoStep={snapshot.project.preview ? demoStep : null}
      />
      <PageFooter />
    </div>
  );
}

export { FeedErrorBanner, Loading } from "./components/AppChrome.jsx";
export { SingleRepoBody } from "./components/train/SingleRepoBody.jsx";
