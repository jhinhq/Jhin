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
 * the transcript polls every 2s on top of that.
 *
 * Sized against the longest thing a spec waits for, which is not an ordinary
 * turn but a deliberately-stretched one: `live-run.ts` builds a ~18s window
 * out of tool steps so the mid-run controls can be pressed, and a spec that
 * waits for that run to *finish* pays the whole window plus a poll. At 45s
 * that left barely 2.5x headroom and a loaded machine could overrun it, which
 * reads as a product failure when it is only slowness. A generous ceiling
 * costs nothing when things are healthy -- the assertion resolves as soon as
 * the reply lands -- and only spends the extra seconds when something is
 * already wrong. */
export const REPLY_TIMEOUT_MS = 90_000;

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
