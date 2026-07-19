import { defineConfig } from "@playwright/test";

const username = process.env.CF_UI_USERNAME ?? "";
const password = process.env.CF_UI_PASSWORD ?? "";

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "runtime/playwright/results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:8080",
    httpCredentials: username && password ? { username, password } : undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
