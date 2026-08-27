/** Starting a chat from the chats home and getting an answer back. */

import { echoOf } from "./fixtures/api";
import { expect, REPLY_TIMEOUT_MS, test } from "./fixtures/test";

test("a message sent from the chats home comes back answered", async ({ page, workspace }) => {
  const scout = await workspace.createAgent("Scout");
  const question = "What did the rollout change?";

  await page.goto("/chats");
  await page.getByRole("radio", { name: scout.name }).click();

  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill(question);
  await composer.press("Enter");

  // Sending from the home screen creates the conversation and redirects into
  // it, carrying the message along.
  await expect(page).toHaveURL(/\/chats\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId("user-message")).toContainText(question);

  await expect(page.getByTestId("agent-message")).toContainText(echoOf(question), {
    timeout: REPLY_TIMEOUT_MS,
  });
  // One question, one answer: no stray second bubble alongside it.
  await expect(page.getByTestId("agent-message")).toHaveCount(1);
});
