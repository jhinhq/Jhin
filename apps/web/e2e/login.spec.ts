/**
 * The one spec that goes through the real sign-in form. Everywhere else the
 * session is injected as cookies, so without this nothing would ever exercise
 * the login page, the CSRF cookie it sets, or the redirect out of it.
 */

import { expect, test } from "./fixtures/test";

test("signing in with the form opens the workspace", async ({ page, context, workspace }) => {
  await workspace.createAgent("Scout");
  // Drop the injected session: this spec is about earning one.
  await context.clearCookies();

  await page.goto("/login");
  await page.getByLabel("Email").fill(workspace.signIn.email);
  await page.getByLabel("Password").fill(workspace.signIn.password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/home$/);
  await expect(page.getByText(workspace.name, { exact: true }).first()).toBeVisible();

  // And the session is good for more than the page that minted it: chats load,
  // which means the CSRF cookie and the session cookie both survived.
  await page.goto("/chats");
  await expect(page.getByRole("radio", { name: "Scout" })).toBeVisible();
});
