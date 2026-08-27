/**
 * Two halves of one rule (components/chat/transcript.tsx): an agent writes
 * markdown and it is rendered; a person's own message is shown exactly as
 * typed, because formatting it would eat the characters they meant.
 *
 * The fake provider echoes the message back, so one turn carrying markdown
 * puts the same characters on both sides of that rule at once.
 */

import { expect, REPLY_TIMEOUT_MS, test } from "./fixtures/test";

// Kept short: the provider echoes only the first 200 characters back.
const SOURCE = ["Formats:", "**bolded** and `inline`", "- first", "- second", "```", "x = 1", "```"].join(
  "\n",
);

test("markdown in an agent reply renders, and the same text stays literal in the user's bubble", async ({
  page,
  workspace,
}) => {
  const scout = await workspace.createAgent("Scout");
  const conversation = await workspace.startConversation(scout, SOURCE, "Formatting");

  await page.goto(`/chats/${conversation.id}`);
  const reply = page.getByTestId("agent-message");
  await expect(reply).toContainText("Formats:", { timeout: REPLY_TIMEOUT_MS });

  await expect(reply.locator("strong")).toHaveText("bolded");
  await expect(reply.locator("code").first()).toHaveText("inline");
  await expect(reply.locator("li")).toHaveText(["first", "second"]);
  await expect(reply.locator("pre code")).toHaveText("x = 1");

  // The user's own bubble keeps the asterisks, the hyphens and the fence.
  const sent = page.getByTestId("user-message");
  await expect(sent.locator("strong")).toHaveCount(0);
  await expect(sent.locator("li")).toHaveCount(0);
  await expect(sent).toContainText("**bolded** and `inline`");
});
