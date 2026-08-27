/**
 * The `test` every chat spec imports: a freshly provisioned workspace, and a
 * browser context already signed in to it.
 *
 * Signing in happens through the API and the cookies are injected, because
 * typing a password into the form on every spec would test the login page
 * dozens of times and the thing under test never. `login.spec.ts` covers the
 * real form once, on purpose.
 */

/* eslint-disable react-hooks/rules-of-hooks --
 * Playwright hands each fixture a `use` callback to yield its value through.
 * There is no React in this directory; the rule only matches on the name. */

import { test as base } from "@playwright/test";
import { provisionWorkspace, type Workspace } from "./api";

/** An agent run has to reach Temporal, a worker, and the fake provider, and
 * the transcript polls every 2s on top of that. */
export const REPLY_TIMEOUT_MS = 45_000;

export const test = base.extend<{ workspace: Workspace }>({
  workspace: async ({ playwright, baseURL }, use, testInfo) => {
    if (!baseURL) throw new Error("baseURL is required; see playwright.config.ts");
    const workspace = await provisionWorkspace(playwright, baseURL, testInfo.title);
    await use(workspace);
    // The workspace itself is left behind deliberately — see e2e/README.md.
    await workspace.client.dispose();
  },

  context: async ({ context, workspace }, use) => {
    await context.addCookies(await workspace.client.cookies());
    await use(context);
  },
});

export { expect } from "@playwright/test";
