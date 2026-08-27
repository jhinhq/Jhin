/**
 * Guard for `make test-e2e`.
 *
 * Playwright ships the driver through npm and the browser separately, so a
 * fresh `pnpm install` gets you a runner with nothing to run. Left alone that
 * surfaces as a stack trace out of the first worker; this turns it into the
 * one line that fixes it.
 */

import { existsSync } from "node:fs";

const INSTALL = "pnpm --filter jhin-web exec playwright install chromium";

let executable;
try {
  const { chromium } = await import("@playwright/test");
  executable = chromium.executablePath();
} catch (error) {
  console.error(`Playwright is not usable here: ${error.message}`);
  console.error("Run:  pnpm install");
  process.exit(1);
}

if (!existsSync(executable)) {
  console.error(`Playwright's Chromium is not downloaded (expected at ${executable}).`);
  console.error(`Run:  ${INSTALL}`);
  process.exit(1);
}
