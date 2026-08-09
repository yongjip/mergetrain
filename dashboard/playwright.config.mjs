import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./test/browser",
  fullyParallel: false,
  retries: 0,
  timeout: 20_000,
  use: {
    baseURL: "http://127.0.0.1:4178",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node test/browser/server.mjs",
    port: 4178,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
