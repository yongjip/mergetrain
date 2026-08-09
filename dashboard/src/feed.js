export const INITIAL_FEED_STATE = Object.freeze({
  snapshot: null,
  error: null,
  lastSuccessAt: null,
});

function normalizedError(payload) {
  const error = payload?.error;
  if (typeof error === "string") {
    return {
      code: "snapshot_unavailable",
      message: error || "The live dashboard snapshot is unavailable.",
      retryable: true,
    };
  }
  return {
    code: error?.code || "snapshot_unavailable",
    message: error?.message || "The live dashboard snapshot is unavailable.",
    retryable: error?.retryable !== false,
  };
}

export function nextFeedState(previous = INITIAL_FEED_STATE, payload, observedAt = new Date().toISOString()) {
  if (payload?.ok === true) {
    return {
      snapshot: payload,
      error: null,
      lastSuccessAt: payload.generated_at || observedAt,
    };
  }
  return {
    ...previous,
    error: normalizedError(payload),
  };
}

export function connectionLabel(connection) {
  return {
    live: "CONNECTED",
    polling: "POLLING",
    degraded: "DEGRADED",
    offline: "DISCONNECTED",
  }[connection] || "CONNECTING";
}
