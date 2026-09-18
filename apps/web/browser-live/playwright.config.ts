import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".", testMatch: "*.spec.ts", outputDir: "../test-results/live-workspace",
  workers: 1, retries: 0, timeout: 180_000,
  use: { baseURL: process.env.JHIN_LIVE_URL ?? "http://localhost:3000", storageState: process.env.JHIN_LIVE_AUTH_STATE, browserName: "chromium", viewport: { width: 1440, height: 1000 }, actionTimeout: 30_000, screenshot: "only-on-failure", trace: "off" },
});
