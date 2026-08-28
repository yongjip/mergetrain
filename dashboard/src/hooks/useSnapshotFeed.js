import { useEffect, useState } from "react";

import { reconnectDelay } from "../dashboardLogic.js";
import { INITIAL_FEED_STATE, nextFeedState } from "../feed.js";

export function readRepoHash() {
  const match = window.location.hash.match(/^#repo=(.+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

export function useSnapshotFeed() {
  const [feed, setFeed] = useState(INITIAL_FEED_STATE);
  const [connection, setConnection] = useState("connecting");

  useEffect(() => {
    let active = true;
    let polling = null;
    let staleTimer = null;
    let lastLiveAt = 0;
    const update = (payload) => {
      if (active) setFeed((current) => nextFeedState(current, payload));
      return payload?.ok === true;
    };
    const fetchSnapshot = async () => {
      try {
        const response = await fetch("/api/snapshot", { cache: "no-store" });
        const healthy = update(await response.json());
        if (active && !healthy) setConnection("degraded");
        if (active && healthy) {
          setConnection((current) => ["offline", "degraded"].includes(current) ? "polling" : current);
        }
      } catch {
        update({
          ok: false,
          error: {
            code: "dashboard_unreachable",
            message: "The dashboard could not read the local train state.",
            retryable: true,
          },
        });
        if (active) setConnection("offline");
      }
    };
    const stopPolling = () => {
      if (polling) window.clearInterval(polling);
      polling = null;
    };
    const markOpen = () => {
      lastLiveAt = Date.now();
      if (staleTimer) window.clearTimeout(staleTimer);
      staleTimer = null;
      stopPolling();
    };
    const markLive = () => {
      markOpen();
      if (active) setConnection("live");
    };
    const startPolling = () => {
      if (!active) return;
      setConnection("polling");
      if (!polling) polling = window.setInterval(fetchSnapshot, 2000);
    };
    fetchSnapshot();
    const source = new EventSource("/api/events");
    source.onopen = markOpen;
    source.addEventListener("snapshot", (event) => {
      try {
        const healthy = update(JSON.parse(event.data));
        if (healthy) markLive();
        else if (active) setConnection("degraded");
      } catch {
        update({
          ok: false,
          error: {
            code: "invalid_snapshot",
            message: "The dashboard received an invalid live-state update.",
            retryable: true,
          },
        });
        if (active) setConnection("degraded");
      }
    });
    source.onerror = () => {
      if (!active) return;
      const delay = reconnectDelay(lastLiveAt);
      if (delay > 0) {
        if (staleTimer) window.clearTimeout(staleTimer);
        staleTimer = window.setTimeout(startPolling, delay);
        return;
      }
      startPolling();
    };
    return () => {
      active = false;
      source.close();
      stopPolling();
      if (staleTimer) window.clearTimeout(staleTimer);
    };
  }, []);

  return { ...feed, connection };
}
