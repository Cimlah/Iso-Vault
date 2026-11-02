import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  timeout: 60_000,
  workers: 1,
  reporter: "list",
  use: { baseURL: process.env.UI_URL, trace: "retain-on-failure" },
  outputDir: process.env.UI_OUTPUT || "test-results",
});
