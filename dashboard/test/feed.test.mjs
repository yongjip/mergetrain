import assert from "node:assert/strict";
import test from "node:test";

import {
  INITIAL_FEED_STATE,
  connectionLabel,
  nextFeedState,
} from "../src/feed.js";

test("a failed snapshot preserves the last good state and records degradation", () => {
  const healthy = {
    ok: true,
    generated_at: "2026-08-09T06:00:00Z",
    jobs: [{ id: 7, status: "in_progress" }],
  };
  const live = nextFeedState(INITIAL_FEED_STATE, healthy);
  const degraded = nextFeedState(live, {
    ok: false,
    error: {
      code: "snapshot_unavailable",
      message: "snapshot failed",
      retryable: true,
    },
  });

  assert.equal(degraded.snapshot, healthy);
  assert.equal(degraded.lastSuccessAt, healthy.generated_at);
  assert.deepEqual(degraded.error, {
    code: "snapshot_unavailable",
    message: "snapshot failed",
    retryable: true,
  });
});

test("a recovered snapshot clears degradation and replaces stale state", () => {
  const stale = nextFeedState(INITIAL_FEED_STATE, {
    ok: true,
    generated_at: "2026-08-09T06:00:00Z",
    jobs: [{ id: 7, status: "in_progress" }],
  });
  const degraded = nextFeedState(stale, { ok: false, error: "temporarily unavailable" });
  const recoveredPayload = {
    ok: true,
    generated_at: "2026-08-09T06:01:00Z",
    jobs: [{ id: 7, status: "validated" }],
  };
  const recovered = nextFeedState(degraded, recoveredPayload);

  assert.equal(recovered.snapshot, recoveredPayload);
  assert.equal(recovered.error, null);
  assert.equal(recovered.lastSuccessAt, recoveredPayload.generated_at);
});

test("connection labels distinguish transport loss from invalid live data", () => {
  assert.equal(connectionLabel("live"), "CONNECTED");
  assert.equal(connectionLabel("polling"), "POLLING");
  assert.equal(connectionLabel("degraded"), "DEGRADED");
  assert.equal(connectionLabel("offline"), "DISCONNECTED");
  assert.equal(connectionLabel("connecting"), "CONNECTING");
});
