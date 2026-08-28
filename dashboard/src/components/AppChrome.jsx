import {
  Bell,
  BellSlash,
  CalendarBlank,
  Clock,
  FileCode,
  GitBranch,
  Moon,
  Play,
  SpinnerGap,
  StackSimple,
  Sun,
  WarningCircle,
  WifiHigh,
} from "@phosphor-icons/react";

import { connectionLabel } from "../feed.js";
import { clockTime, relative } from "../dashboardFormatters.js";

export function NotificationControl({ notifications }) {
  const copy = {
    on: ["Notifications on", "Disable dashboard notifications"],
    off: ["Enable notifications", "Enable browser notifications while this dashboard is open"],
    blocked: ["Notifications blocked", "Allow site notifications or open this dashboard in a browser that supports them"],
    unsupported: ["Notifications unavailable", "Use a secure dashboard URL in a browser that supports site notifications"],
  }[notifications.state];
  const unavailable = ["blocked", "unsupported"].includes(notifications.state);
  return (
    <button
      aria-label={copy[1]}
      aria-pressed={unavailable ? undefined : notifications.state === "on"}
      className={`notification-toggle ${notifications.state}`}
      disabled={unavailable}
      onClick={notifications.toggle}
      title={copy[1]}
      type="button"
    >
      {notifications.state === "on"
        ? <Bell size={17} weight="fill" />
        : <BellSlash size={17} />}
      <span>{copy[0]}</span>
    </button>
  );
}

export function Header({
  snapshot,
  connection,
  now,
  hub,
  repoName,
  notifications,
  theme,
  onToggleTheme,
  demoState,
  onPlayDemo,
}) {
  const generated = relative(snapshot.generated_at, now);
  const connectionText = connectionLabel(connection);
  const preview = !hub && snapshot.project?.preview;
  return (
    <header className="topbar">
      <div className="brand"><StackSimple size={34} weight="bold" /><strong>mergetrain</strong>{hub && <span className="hub-badge">HUB</span>}</div>
      {hub ? (
        repoName
          ? <div className="context"><FileCode size={19} /><span>{repoName}</span></div>
          : <div className="context"><StackSimple size={19} /><span>{snapshot.repo_count} repo{snapshot.repo_count === 1 ? "" : "s"}</span></div>
      ) : (
        <>
          <div className="context"><FileCode size={19} /><span>{snapshot.project.name}</span></div>
          <div className="context"><GitBranch size={19} /><span>{snapshot.project.integration_ref}</span></div>
        </>
      )}
      <span className="local-badge">LOCAL</span>
      {preview && <span className="preview-badge">DEMO DATA</span>}
      <div className="topbar-spacer" />
      {preview && (
        <button
          className={`demo-play ${demoState?.playing ? "playing" : ""}`}
          type="button"
          onClick={onPlayDemo}
          aria-label={demoState?.playing ? `Playing demo step ${demoState.step + 1} of 7` : "Play demo"}
          disabled={demoState?.playing}
        >
          {demoState?.playing ? <SpinnerGap size={17} className="spin" /> : <Play size={17} weight="fill" />}
          <span>{demoState?.playing ? `Playing ${demoState.step + 1} / 7` : "Play demo"}</span>
        </button>
      )}
      <div className={`live ${connection}`}><span className="live-dot" />{connectionText}<small>· updated {generated}</small></div>
      <NotificationControl notifications={notifications} />
      <button
        className="theme-toggle"
        type="button"
        onClick={onToggleTheme}
        aria-label={`Use ${theme === "dark" ? "light" : "dark"} theme`}
        title={`Use ${theme === "dark" ? "light" : "dark"} theme`}
      >
        {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
      </button>
      <div className="context divider"><Clock size={19} /><span>{clockTime(now)}</span></div>
      <div className="context divider"><CalendarBlank size={19} /><span>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(now)}</span></div>
    </header>
  );
}

export function Loading({ error = null }) {
  if (error) {
    return (
      <main className="loading loading-error" role="alert">
        <WarningCircle size={36} weight="fill" />
        <div>
          <strong>Local train state unavailable</strong>
          <span>{error.message} Retrying automatically.</span>
        </div>
      </main>
    );
  }
  return <main className="loading"><SpinnerGap size={36} className="spin" /><strong>Reading local train state…</strong></main>;
}

export function RegistryErrorBanner({ message }) {
  return (
    <div className="registry-error-banner" role="alert">
      <WarningCircle size={18} weight="fill" />
      <strong>Registry unreadable</strong>
      <span>{message}</span>
    </div>
  );
}

export function FeedErrorBanner({ error, lastSuccessAt, now }) {
  if (!error) return null;
  const lastKnown = lastSuccessAt ? ` Last good snapshot: ${relative(lastSuccessAt, now)}.` : "";
  return (
    <div className="feed-error-banner" role="alert">
      <WarningCircle size={18} weight="fill" />
      <strong>Live state unavailable</strong>
      <span>{error.message} Showing the last known state.{lastKnown} Retrying automatically.</span>
    </div>
  );
}

export function PageFooter() {
  return (
    <footer className="page-footer">
      <WifiHigh size={18} />
      <span>Read-only local view</span>
      <i>·</i>
      <span>All actions are performed by mergetrain.</span>
    </footer>
  );
}
