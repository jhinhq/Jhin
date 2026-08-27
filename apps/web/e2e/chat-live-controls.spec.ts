/**
 * What a person can do to a run that is still going: stop it, pause and resume
 * it, steer it, and walk away and come back.
 *
 * All four need the agent to still be working when the browser arrives, which
 * `liveRun` arranges — see e2e/fixtures/live-run.ts for why the window is built
 * out of steps rather than out of `FAKE_PROVIDER_LATENCY_MS`.
 */

import type { Locator, Page } from "@playwright/test";
import type { Workspace } from "./fixtures/api";
import { liveRun, providerLatencyMs } from "./fixtures/live-run";
import { expect, REPLY_TIMEOUT_MS, test } from "./fixtures/test";

const THREAD_TITLE = "Live work";

interface LiveChat {
  id: string;
  /** The thread itself. Scoping matters: the conversation rail beside it shows
   * a live-status pill of its own for the same chat. */
  thread: Locator;
}

/** Start a chat whose first turn keeps the agent busy, and open it. */
async function openLiveChat(
  page: Page,
  workspace: Workspace,
  latencyMs: number,
  question: string,
): Promise<LiveChat> {
  const run = liveRun(question, latencyMs);
  const scout = await workspace.createAgent("Scout", {
    grants: run.grants,
    maxSteps: run.maxSteps,
  });
  const conversation = await workspace.startConversation(scout, run.text, THREAD_TITLE);

  await page.goto(`/chats/${conversation.id}`);
  const thread = page.getByRole("region", { name: THREAD_TITLE });
  // The header pill is the UI saying the server told it a run is in flight.
  await expect(thread.getByTestId("live-status")).toHaveAttribute("data-kind", "working", {
    timeout: 30_000,
  });
  return { id: conversation.id, thread };
}

test("Stop ends a run in flight", async ({ page, request, workspace }) => {
  const latency = await providerLatencyMs(request);
  const chat = await openLiveChat(page, workspace, latency, "Take your time counting.");

  await chat.thread.getByTestId("composer-stop").click();
  // Scoped to the confirmation: the composer's own Stop carries the same
  // accessible name, and it is still on the page behind the dialog.
  const confirm = page.getByRole("dialog", { name: "Stop Scout?" });
  await confirm.getByRole("button", { name: "Stop Scout", exact: true }).click();

  // The transcript says so, and so does the task behind it.
  //
  // KNOWN PRODUCT RACE — this assertion is occasionally red under load, and it
  // is the product, not the spec. The "Stopped" chip is derived from the task
  // being cancelled, so it appears in `GET …/activity` at the same instant the
  // conversation stops reporting an active task. That instant is also when
  // `live` goes false in the thread view and both pollers are switched off
  // (app/(app)/chats/[id]/view.tsx), so whether the chip is ever fetched comes
  // down to which 2-second poller ticks first. `recentlySent` normally papers
  // over it, but only for 20s after a send *from this page* — and pressing Stop
  // is not a send. Reloading always shows the chip. Left asserting the right
  // thing: a stop the reader took should leave a record they can see.
  await expect(chat.thread.getByTestId("activity-chip")).toContainText("Stopped", {
    timeout: REPLY_TIMEOUT_MS,
  });
  await expect(chat.thread.getByTestId("live-status")).toHaveCount(0);

  const detail = await workspace.waitUntilIdle(chat.id);
  expect(detail.tasks[0].state).toBe("cancelled");
});

test("Pause offers Resume, and Resume gets the run going again", async ({
  page,
  request,
  workspace,
}) => {
  const latency = await providerLatencyMs(request);
  const chat = await openLiveChat(page, workspace, latency, "Keep counting for a while.");

  await chat.thread.getByRole("button", { name: "Show details" }).click();
  const details = page.getByRole("complementary", { name: "Chat details" });

  await details.getByRole("button", { name: "Pause work" }).click();

  // The Resume control is gated on the *task* being paused, which the API
  // writes onto the row as it signals the workflow. Until that lands the panel
  // still offers Pause, so this assertion is the whole point of the spec.
  await expect(details.getByRole("button", { name: "Resume work" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(chat.thread.getByTestId("live-status")).toHaveAttribute("data-kind", "paused");

  await details.getByRole("button", { name: "Resume work" }).click();
  await expect(details.getByRole("button", { name: "Pause work" })).toBeVisible({
    timeout: 30_000,
  });

  const detail = await workspace.waitUntilIdle(chat.id);
  expect(detail.tasks[0].state).toBe("completed");
});

test("a follow-up sent mid-run steers the same work instead of starting more", async ({
  page,
  request,
  workspace,
}) => {
  const latency = await providerLatencyMs(request);
  const chat = await openLiveChat(page, workspace, latency, "Start counting.");

  const composer = chat.thread.getByRole("textbox", { name: "Message" });
  await composer.fill("Actually, count backwards.");
  await composer.press("Enter");

  // Marked as an instruction the moment it is sent — the agent picks pending
  // instructions up at its next step, so it is queued rather than answered.
  const status = chat.thread.getByTestId("instruction-status");
  await expect(status).toBeVisible();
  await expect(status).toContainText("Queued");

  // And it is a steer, not a second request: still one work episode.
  const beforeFinish = await workspace.conversation(chat.id);
  expect(beforeFinish.tasks).toHaveLength(1);

  await workspace.waitUntilIdle(chat.id);
  await expect(status).toHaveAttribute("data-state", "delivered", { timeout: REPLY_TIMEOUT_MS });
  await expect(status).toContainText("Steered Scout");

  const afterFinish = await workspace.conversation(chat.id);
  expect(afterFinish.tasks).toHaveLength(1);
});

test("reloading mid-run picks the live run back up from the server", async ({
  page,
  request,
  workspace,
}) => {
  const latency = await providerLatencyMs(request);
  const chat = await openLiveChat(page, workspace, latency, "Count the whole shipment.");

  await page.reload();

  // Nothing survives a reload in the browser, so everything on screen after it
  // came back from the API: the message that started the run, and the fact
  // that it is still going.
  await expect(chat.thread.getByTestId("user-message")).toContainText("Count the whole shipment.");
  await expect(chat.thread.getByTestId("working-indicator")).toBeVisible();
  await expect(chat.thread.getByTestId("live-status")).toHaveAttribute("data-kind", "working");

  // Then it finishes, on the reloaded page.
  await expect(chat.thread.getByTestId("agent-message")).toHaveCount(1, {
    timeout: REPLY_TIMEOUT_MS,
  });
  await expect(chat.thread.getByTestId("live-status")).toHaveCount(0);

  const detail = await workspace.waitUntilIdle(chat.id);
  expect(detail.tasks[0].state).toBe("completed");
});
