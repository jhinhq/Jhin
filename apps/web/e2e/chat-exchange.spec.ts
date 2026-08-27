/**
 * Agent-to-agent traffic stays folded away.
 *
 * When the agent a person is talking to hands work to a colleague, the
 * delegation and the colleague's report are company traffic, not part of the
 * dialogue: they collapse into one quiet row the reader can open. A colleague
 * appearing as a loose bubble reads as if they were talking to the reader.
 */

import { expect, REPLY_TIMEOUT_MS, test } from "./fixtures/test";

/** The fake provider turns each `[[tool:…]]` marker in a user message into a
 * tool call. `[[b64:…]]` hides a script one hop deeper: each conversation scan
 * decodes exactly one layer, so the parent sees an inert blob inside its own
 * marker and the delegated child decodes it into the script it runs. */
function b64(script: string): string {
  return `[[b64:${Buffer.from(script, "utf8").toString("base64")}]]`;
}

test("a colleague's work collapses into one row instead of a loose bubble", async ({
  page,
  workspace,
}) => {
  const helper = await workspace.createAgent("Helper", {
    grants: { "organization.report_result": {} },
  });
  const primary = await workspace.createAgent("Primary", {
    grants: { "organization.delegate": { targets: "any" } },
  });

  const childScript =
    '[[tool:organization.report_result {"status": "completed", ' +
    '"summary": "Helper counted them all"}]]';
  const request =
    "Ask Helper to count the crates. " +
    `[[tool:organization.delegate_task {"target_agent_id": "${helper.id}", ` +
    '"title": "Count the crates", ' +
    `"instructions": "Count them. ${b64(b64(childScript))}", ` +
    '"expected_output": "a short confirmation", "blocking": true, "kind": "delegation"}]]';

  const conversation = await workspace.startConversation(primary, request, "Crates");
  await workspace.waitUntilIdle(conversation.id);

  await page.goto(`/chats/${conversation.id}`);

  // The dialogue: the reader's message, and Primary's own answer. Nothing else.
  const exchange = page.getByTestId("exchange");
  await expect(exchange).toHaveCount(1, { timeout: REPLY_TIMEOUT_MS });
  await expect(page.getByTestId("agent-message")).toHaveCount(1);

  // The delegation out and the report back are inside the collapsed row, so
  // neither is rendered until it is opened.
  await expect(page.getByTestId("work-card")).toHaveCount(0);
  await expect(exchange).toContainText("2 updates with Helper");

  await exchange.getByRole("button", { expanded: false }).click();
  const cards = page.getByTestId("work-card");
  await expect(cards).toHaveCount(2);
  await expect(cards.first()).toContainText("Asked Helper for help");
  await expect(cards.last()).toContainText("Helper counted them all");
});
