import { defineConfig, devices } from "@playwright/test";

/**
 * Browser end-to-end specs for the chat experience, run against a live dev
 * stack (`docker compose -f compose.yaml -f compose.dev.yaml up -d`).
 *
 * Deliberately not part of `pnpm test`: that gate is Vitest against jsdom, and
 * it stays fast and offline. Run these with `make test-e2e`, which checks the
 * browser is installed first, or `pnpm --filter jhin-web test:e2e`.
 *
 * There is no `webServer` block. The app under test is the containerised one,
 * with the API, the workers, Temporal and the fake provider behind it; a
 * Playwright-started `next dev` would be a different program.
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  // A chat turn is a real run through Temporal and a worker, and the mid-run
  // specs deliberately keep one going for ~18s.
  timeout: 150_000,
  expect: { timeout: 15_000 },
  // Each spec owns its workspace, so they do not collide — but they share one
  // stack with each other and with whatever else is using the machine, and
  // every extra worker is another real Temporal workflow on it. Two runs the
  // suite in well under a minute; more buys seconds and costs headroom.
  workers: Number(process.env.JHIN_E2E_WORKERS ?? 2),
  fullyParallel: true,
  // No retries: a spec that only passes on the second go is telling us
  // something, and hiding it here is how that stops being heard.
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: process.env.JHIN_E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  projects: [{ name: "chromium" }],
});
