import { useEffect, useRef, useState } from "react";

import {
  claimNotification,
  feedErrorCandidate,
  notificationCandidates,
  readNotificationPreference,
  writeNotificationPreference,
} from "../notifications.js";

export function browserNotificationPermission() {
  if (typeof window === "undefined" || !window.isSecureContext || !("Notification" in window)) {
    return "unsupported";
  }
  return window.Notification.permission;
}

function showBrowserNotification(candidate) {
  try {
    const notification = new window.Notification(candidate.title, {
      body: candidate.body,
      icon: "/favicon.svg",
      requireInteraction: candidate.kind === "attention",
      tag: candidate.id,
    });
    notification.onclick = () => {
      if (candidate.repoPath) {
        window.location.hash = `repo=${encodeURIComponent(candidate.repoPath)}`;
      }
      window.focus();
      notification.close();
    };
    return true;
  } catch {
    return false;
  }
}

async function deliverBrowserNotification(candidate) {
  const claim = () => claimNotification(window.localStorage, candidate.id);
  let claimed = false;
  if (window.navigator.locks?.request) {
    try {
      await window.navigator.locks.request(
        "mergetrain-dashboard-notification",
        () => { claimed = claim(); },
      );
    } catch {
      claimed = claim();
    }
  } else {
    claimed = claim();
  }
  if (claimed) showBrowserNotification(candidate);
}

export function useDashboardNotifications(snapshot, feedError) {
  const [enabled, setEnabled] = useState(
    () => typeof window !== "undefined" && readNotificationPreference(window.localStorage),
  );
  const [permission, setPermission] = useState(browserNotificationPermission);
  const previousSnapshot = useRef(null);
  const previousFeedError = useRef(null);

  const saveEnabled = (value) => {
    setEnabled(value);
    writeNotificationPreference(window.localStorage, value);
  };

  useEffect(() => {
    const refreshPermission = () => {
      const value = browserNotificationPermission();
      setPermission(value);
      if (["denied", "unsupported"].includes(value)) saveEnabled(false);
    };
    document.addEventListener("visibilitychange", refreshPermission);
    return () => document.removeEventListener("visibilitychange", refreshPermission);
  }, []);

  useEffect(() => {
    if (!snapshot) return;
    const previous = previousSnapshot.current;
    previousSnapshot.current = snapshot;
    if (!previous || !enabled || permission !== "granted") return;
    notificationCandidates(previous, snapshot).forEach((candidate) => {
      void deliverBrowserNotification(candidate);
    });
  }, [snapshot, enabled, permission]);

  useEffect(() => {
    const previous = previousFeedError.current;
    previousFeedError.current = feedError;
    if (!snapshot || !feedError || previous || !enabled || permission !== "granted") return;
    const candidate = feedErrorCandidate(snapshot, feedError);
    if (candidate) void deliverBrowserNotification(candidate);
  }, [snapshot, feedError, enabled, permission]);

  const toggle = async () => {
    if (permission === "unsupported" || permission === "denied") return;
    if (enabled && permission === "granted") {
      saveEnabled(false);
      return;
    }
    let nextPermission = permission;
    if (nextPermission !== "granted") {
      try {
        nextPermission = await window.Notification.requestPermission();
      } catch {
        nextPermission = browserNotificationPermission();
      }
      setPermission(nextPermission);
    }
    if (nextPermission !== "granted") {
      saveEnabled(false);
      return;
    }
    saveEnabled(true);
    showBrowserNotification({
      id: "mergetrain-dashboard-notifications-enabled",
      title: "mergetrain notifications enabled",
      body: "This dashboard will alert you when a train finishes or needs attention.",
      kind: "enabled",
    });
  };

  const state = permission === "unsupported"
    ? "unsupported"
    : permission === "denied"
      ? "blocked"
      : enabled && permission === "granted"
        ? "on"
        : "off";
  return { state, toggle };
}
