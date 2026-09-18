import { expect, test, type Locator, type Page } from "@playwright/test";
import type { ApiKeyInfo, ScopeCatalog } from "../lib/types";

const base = "/api/v1/workspaces/fixture-workspace/api-keys";
const catalog: ScopeCatalog = {
  your_role: "member",
  categories: Array.from({ length: 20 }, (_, index) => {
    const key = `category-${index + 1}`;
    return {
      key, label: `Category ${index + 1}`, description: "Workspace resources and administration.",
      scopes: ["read", "write", "admin"].map((action) => ({
        key: `${key}:${action}`, category: key, action,
        label: `${action === "read" ? "Read" : action === "write" ? "Change" : "Administer"} resources ${index + 1}`,
        description: "Allow this operation for the selected workspace resources.",
        min_role: action === "admin" || index === 19 ? "owner" : "member",
        available: action !== "admin" && index !== 19,
      })),
    };
  }),
};
const allScopes = catalog.categories.flatMap((category) => category.scopes);
const available = allScopes.filter((scope) => scope.available).map((scope) => scope.key);
const readOnly = allScopes.filter((scope) => scope.available && scope.action === "read").map((scope) => scope.key);
type CreateBody = { name: string; scopes: string[]; expires_unit: string; expires_in: number | null };

async function mockApiKeys(page: Page) {
  const creates: CreateBody[] = [];
  const keys: ApiKeyInfo[] = [];
  const unexpected: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET") {
      if (path === base) return route.fulfill({ json: keys });
      if (path === `${base}/scopes`) return route.fulfill({ json: catalog });
      if (path === `${base}/usage`) return route.fulfill({ json: { items: [], total: 0 } });
    }
    if (request.method() === "POST" && path === base) {
      const body = request.postDataJSON() as CreateBody;
      creates.push(body);
      const key: ApiKeyInfo = {
        id: "fixture-created-key", name: body.name, prefix: "fixture0", scopes: body.scopes,
        role_ceiling: "member", created_by_user_id: "fixture-user", created_by_name: "Fixture User",
        expires_at: "2026-12-09T00:00:00Z", last_used_at: null, revoked_at: null,
        created_at: "2026-09-10T00:00:00Z", status: "active",
      };
      keys.push(key);
      return route.fulfill({ status: 201, json: { api_key: key, key: "fixture-only-not-a-real-api-key" } });
    }
    unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 500, json: { detail: "Unexpected browser fixture request" } });
  });
  return { creates, unexpected, errors };
}

async function openKeyDialog(page: Page) {
  await page.goto("/api-keys.html");
  await page.getByRole("button", { name: "New key", exact: true }).first().click();
  await expect(page.getByRole("dialog", { name: "New API key", exact: true })).toBeVisible();
  await expect(page.getByTestId("scope-tree")).toBeVisible();
}

async function scrollState(control: Locator) {
  return control.evaluate((element) => {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight) {
        return { scrollTop: parent.scrollTop, controlTop: element.getBoundingClientRect().top };
      }
    }
    return { scrollTop: document.scrollingElement?.scrollTop ?? 0, controlTop: element.getBoundingClientRect().top };
  });
}

async function expectVisibleWithinViewport(control: Locator) {
  const box = await control.boundingBox();
  expect(box).not.toBeNull();
  const viewport = control.page().viewportSize()!;
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
}

for (const width of [390, 1440]) {
  for (const kind of ["category", "individual"] as const) {
    test(`lower ${kind} permission keeps focus and scroll at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      const api = await mockApiKeys(page);
      await openKeyDialog(page);
      if (kind === "individual") await page.getByRole("button", { name: "Show Category 18 permissions", exact: true }).click();
      const control = page.locator(kind === "category" ? '[id="scope-cat-category-18"]' : '[id="scope-category-18:read"]');
      await control.scrollIntoViewIfNeeded();
      const before = await scrollState(control);
      expect(before.scrollTop).toBeGreaterThan(300);
      await control.click();
      await expect(control).toBeChecked();
      await expect(control).toBeFocused();
      const after = await scrollState(control);
      expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThan(3);
      expect(Math.abs(after.controlTop - before.controlTop)).toBeLessThan(3);
      expect(api.creates).toEqual([]);
      expect(api.unexpected).toEqual([]);
      expect(api.errors).toEqual([]);
    });
  }

  test(`full access presets and pinned creation footer at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const api = await mockApiKeys(page);
    await openKeyDialog(page);
    const dialog = page.getByRole("dialog", { name: "New API key", exact: true });
    await dialog.getByPlaceholder("Nightly report script").fill("Browser full access");
    const create = dialog.getByRole("button", { name: "Create key", exact: true });
    await dialog.getByRole("button", { name: "Full access", exact: true }).click();
    await expect(page.getByTestId("scope-count")).toHaveText(`${available.length} permissions selected`);
    await expect(create).toBeEnabled();
    await expectVisibleWithinViewport(create);
    await page.screenshot({ path: testInfo.outputPath("api-key-presets.png") });
    await dialog.getByRole("button", { name: /^Read.only$/ }).click();
    await expect(page.getByTestId("scope-count")).toHaveText(`${readOnly.length} permissions selected`);
    await dialog.getByRole("button", { name: "Clear", exact: true }).click();
    await expect(page.getByTestId("scope-count")).toHaveText("Choose at least one permission.");
    await expect(create).toBeDisabled();
    await dialog.getByRole("button", { name: "Full access", exact: true }).click();
    await dialog.getByRole("button", { name: "Show Category 20 permissions", exact: true }).click();
    for (const scope of catalog.categories[19].scopes) {
      const unavailable = page.locator(`[id="scope-${scope.key}"]`);
      await expect(unavailable).toBeDisabled();
      await expect(unavailable).not.toBeChecked();
    }
    await page.locator('[id="scope-category-20:admin"]').scrollIntoViewIfNeeded();
    await expectVisibleWithinViewport(create);
    await expectVisibleWithinViewport(dialog.getByRole("button", { name: "Cancel", exact: true }));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("api-key-permissions.png"), fullPage: true });
    await create.click();
    await expect(page.getByTestId("api-key-reveal")).toContainText("fixture-only-not-a-real-api-key");
    await expect(page.getByRole("button", { name: "Copy", exact: true })).toBeFocused();
    expect(api.creates).toHaveLength(1);
    expect(api.creates[0]).toEqual({ name: "Browser full access", scopes: available, expires_unit: "days", expires_in: 90 });
    expect(api.creates[0].scopes).not.toContain("*");
    expect(api.unexpected).toEqual([]);
    expect(api.errors).toEqual([]);
  });
}
