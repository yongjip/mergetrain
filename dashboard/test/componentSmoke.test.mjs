import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const dashboardRoot = new URL("../", import.meta.url);

async function fixture(name) {
  return JSON.parse(
    await readFile(new URL(`fixtures/${name}.json`, import.meta.url), "utf8"),
  );
}

test("single-repo components render shared running and validated snapshots", async () => {
  const vite = await createServer({
    root: dashboardRoot.pathname,
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  try {
    const { FeedErrorBanner, Loading, SingleRepoBody } = await vite.ssrLoadModule("/src/App.jsx");
    const now = new Date("2026-07-29T12:00:35Z");
    const running = renderToStaticMarkup(
      React.createElement(SingleRepoBody, {
        snapshot: await fixture("running-snapshot"),
        now,
      }),
    );
    assert.match(running, /~25s left/);
    assert.match(running, /Gate timing/);
    assert.match(running, /Show 10 more|Activity/);
    assert.match(running, /Phone glance status/);
    assert.match(running, /1 request blocked/);

    const validated = renderToStaticMarkup(
      React.createElement(SingleRepoBody, {
        snapshot: await fixture("validated-snapshot"),
        now: new Date("2026-07-29T13:00:00Z"),
      }),
    );
    assert.match(validated, /Awaiting deploy approval/);
    assert.match(validated, /Tests passed · Not on main yet/);
    assert.match(validated, /train-fixture/);

    const stale = renderToStaticMarkup(
      React.createElement(FeedErrorBanner, {
        error: { message: "snapshot failed" },
        lastSuccessAt: "2026-07-29T12:00:00Z",
        now,
      }),
    );
    assert.match(stale, /Live state unavailable/);
    assert.match(stale, /Showing the last known state/);
    assert.match(stale, /35s ago/);

    const initialFailure = renderToStaticMarkup(
      React.createElement(Loading, { error: { message: "snapshot failed" } }),
    );
    assert.match(initialFailure, /Local train state unavailable/);
    assert.match(initialFailure, /Retrying automatically/);
  } finally {
    await vite.close();
  }
});
