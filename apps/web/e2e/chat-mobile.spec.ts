/**
 * The thread header on a phone.
 *
 * The header wraps its controls onto a second row below `sm`, and the title
 * block holds its ground with `min-w-0 flex-1 basis-0` + `truncate`. Lose the
 * wrap and the title is squeezed to nothing between the avatar and the
 * buttons; lose the truncation and a long title spills onto extra lines and
 * pushes the composer down the screen. Both are checked here.
 */

import { expect, test } from "./fixtures/test";

const IPHONE_WIDTH = 375;

test.use({ viewport: { width: IPHONE_WIDTH, height: 812 } });

test("the chat header keeps a readable title at 375px", async ({ page, workspace }) => {
  const scout = await workspace.createAgent("Scout");
  // Longer than the viewport on purpose: a title that fits proves nothing
  // about a header that has to make one fit.
  const title = "Quarterly logistics review for the northern distribution corridor";
  const conversation = await workspace.startConversation(scout, "Where are we?", title);

  await page.goto(`/chats/${conversation.id}`);

  const heading = page.getByRole("button", { name: `Rename chat: ${title}` });
  await expect(heading).toBeVisible();
  await expect(heading).toContainText(title);

  const box = (await heading.boundingBox())!;

  // Not collapsed: enough width left to read words in, not just an ellipsis.
  // Below `sm` the header wraps its controls onto a row of their own, which is
  // the only reason there is room; drop that and the title is squeezed to
  // nothing between the avatar and four icon buttons.
  expect(box.width).toBeGreaterThan(IPHONE_WIDTH / 2);

  // And still one line. `truncate` is what keeps a title longer than the
  // viewport from wrapping and shoving the conversation down the page, and
  // width alone cannot see that -- a wrapped title is just as wide.
  const lineHeight = await heading.evaluate(
    (node) => Number.parseFloat(getComputedStyle(node).lineHeight) || 0,
  );
  expect(lineHeight).toBeGreaterThan(0);
  expect(box.height).toBeLessThan(lineHeight * 2);

  // The composer is still usable underneath it.
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
});
