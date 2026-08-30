/**
 * The regression this suite exists for: an agent answering the *previous*
 * question.
 *
 * `build_messages` used to put the current question at index 1 and append the
 * history after it, and the worker deleted the current task's seed user
 * message — so the newest user message the model saw was the turn before.
 * Fixed in a6d9a4e / ab3c995, where a chat turn stopped composing the
 * "Task: …" brief and the seed message kept its chronological place.
 *
 * The invariant: the newest user turn is the last user-role message the
 * provider sees. The fake provider echoes back whichever user message that
 * was, so the reply names the question the model actually read — and three
 * sequential turns with distinguishable questions catch an off-by-one
 * directly, no matter how it got there.
 */

import { expect, REPLY_TIMEOUT_MS, test } from "./fixtures/test";

const QUESTIONS = [
  "Turn one: which harbour did the alpha shipment leave from?",
  "Turn two: which harbour did the bravo shipment leave from?",
  "Turn three: which harbour did the charlie shipment leave from?",
];

test("the third reply answers the third question", async ({ page, workspace }) => {
  const scout = await workspace.createAgent("Scout");
  const conversation = await workspace.startConversation(scout, QUESTIONS[0], "Shipments");
  await workspace.waitUntilIdle(conversation.id);

  await page.goto(`/chats/${conversation.id}`);
  const replies = page.getByTestId("agent-message");
  await expect(replies).toHaveCount(1, { timeout: REPLY_TIMEOUT_MS });

  // Turns two and three go through the composer, one at a time: each waits for
  // its answer, so each is a new work episode rather than a mid-run steer.
  const composer = page.getByRole("textbox", { name: "Message" });
  for (const [index, question] of QUESTIONS.slice(1).entries()) {
    await composer.fill(question);
    await composer.press("Enter");
    await expect(replies).toHaveCount(index + 2, { timeout: REPLY_TIMEOUT_MS });
  }

  // Every answer names its own question, in order. The bug showed up as reply
  // n echoing question n-1, so asserting all three pins the alignment rather
  // than only the endpoint. Asserted as "contains the question", not the
  // exact echo: a turn that lands while the previous run is wrapping up is
  // (correctly) delivered as a drained instruction, and the worker prefixes
  // those with "Additional instruction:" in the prompt.
  for (const [index, question] of QUESTIONS.entries()) {
    await expect(replies.nth(index)).toContainText(question);
  }

  // Said the other way round, because this is the shape the bug took: the last
  // answer must not be about the question before it.
  await expect(replies.last()).not.toContainText("bravo shipment");
});
