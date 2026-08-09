import { createServer as createHttpServer } from "node:http";
import { readFile } from "node:fs/promises";

import { createServer as createViteServer } from "vite";

const root = new URL("../../", import.meta.url);
const runningFixture = JSON.parse(
  await readFile(new URL("../fixtures/running-snapshot.json", import.meta.url), "utf8"),
);
const validatedFixture = JSON.parse(
  await readFile(new URL("../fixtures/validated-snapshot.json", import.meta.url), "utf8"),
);
const running = {
  ...runningFixture,
  project: { ...runningFixture.project, name: "api" },
};
const validated = {
  ...validatedFixture,
  project: { ...validatedFixture.project, name: "api" },
};

function hubSnapshot(repoSnapshot) {
  return {
    ok: true,
    hub: true,
    generated_at: repoSnapshot.generated_at,
    repo_count: 1,
    repos: [
      {
        path: "/work/api",
        name: "api",
        daemon_enabled: true,
        ok: true,
        empty: false,
        snapshot: repoSnapshot,
      },
    ],
  };
}

const fixtures = {
  running: hubSnapshot(running),
  validated: hubSnapshot(validated),
  error: {
    ok: false,
    error: {
      code: "snapshot_unavailable",
      message: "snapshot temporarily unavailable",
      retryable: true,
    },
  },
};

let current = fixtures.running;
const clients = new Set();

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
    "content-type": "application/json; charset=utf-8",
  });
  response.end(body);
}

function broadcast(payload) {
  const frame = `event: snapshot\ndata: ${JSON.stringify(payload)}\n\n`;
  clients.forEach((response) => response.write(frame));
}

const vite = await createViteServer({
  root: root.pathname,
  appType: "spa",
  logLevel: "error",
  server: { hmr: false, middlewareMode: true },
});

const server = createHttpServer((request, response) => {
  const url = new URL(request.url || "/", "http://127.0.0.1");
  if (request.method === "GET" && url.pathname === "/api/snapshot") {
    sendJson(response, current.ok ? 200 : 503, current);
    return;
  }
  if (request.method === "GET" && url.pathname === "/api/events") {
    response.writeHead(200, {
      "cache-control": "no-cache",
      connection: "keep-alive",
      "content-type": "text/event-stream; charset=utf-8",
    });
    clients.add(response);
    response.write(`event: snapshot\ndata: ${JSON.stringify(current)}\n\n`);
    request.on("close", () => clients.delete(response));
    return;
  }
  if (request.method === "POST" && url.pathname === "/__test/state") {
    const key = url.searchParams.get("value") || "";
    if (!(key in fixtures)) {
      sendJson(response, 400, { ok: false, error: "unknown fixture" });
      return;
    }
    current = fixtures[key];
    broadcast(current);
    sendJson(response, 200, { ok: true, state: key });
    return;
  }
  vite.middlewares(request, response, () => {
    sendJson(response, 404, { ok: false, error: "not found" });
  });
});

server.listen(4178, "127.0.0.1");

async function shutdown() {
  clients.forEach((response) => response.end());
  await new Promise((resolve) => server.close(resolve));
  await vite.close();
}

process.once("SIGINT", () => void shutdown().then(() => process.exit(0)));
process.once("SIGTERM", () => void shutdown().then(() => process.exit(0)));
