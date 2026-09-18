import { expect, test, type Page } from "@playwright/test";
import {
  assignmentAgentId,
  assignmentConnection,
  assignmentConnectionTools,
  assignmentTools,
  assignmentWorkspaceId,
} from "./assignment-data";

type GrantRequest = { capability: string; scope: Record<string, string>; effect: string };

async function mockAssignmentApi(page: Page, failOnce?: string) {
  const requests: GrantRequest[] = [];
  const saved = new Map<string, GrantRequest>();
  const unexpected: string[] = [];
  let failed = false;
  const base = `/api/v1/workspaces/${assignmentWorkspaceId}`;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === `${base}/agents/${assignmentAgentId}/grants`) {
      const body = request.postDataJSON() as GrantRequest;
      requests.push(body);
      if (body.capability === failOnce && !failed) {
        failed = true;
        return route.fulfill({ status: 422, json: { detail: "The selected project scope could not be saved. Review this tool and retry." } });
      }
      saved.set(body.capability, body);
      return route.fulfill({ status: 201, json: {
        id: `grant-${body.capability}`, agent_id: assignmentAgentId, capability: body.capability,
        scope_json: body.scope, effect: body.effect, problems: [], created_at: "2026-09-10T00:00:00Z",
      } });
    }
    if (request.method() === "GET") {
      if (path === `${base}/agents`) return route.fulfill({ json: [
        { id: assignmentAgentId, name: "Bisby", status: "active" },
        { id: "disabled-agent", name: "Disabled agent", status: "disabled" },
      ] });
      if (path === `${base}/tools`) return route.fulfill({ json: assignmentTools });
      if (path === `${base}/connections/${assignmentConnection.id}/tools`) return route.fulfill({ json: assignmentConnectionTools });
      if (path === `${base}/connections/${assignmentConnection.id}/access-summary`) return route.fulfill({ json: {
        connection_id: assignmentConnection.id,
        agents: saved.size ? [{
          agent_id: assignmentAgentId,
          agent_name: "Bisby",
          authorized: true,
          authorized_tool_names: [...saved.keys()],
          grants: [...saved.values()].map((grant) => ({
            grant_id: `grant-${grant.capability}`, ...grant,
            eligible_tool_names: [grant.capability], eligibility_reason: null,
          })),
        }] : [],
      } });
    }
    unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 500, json: { detail: "Unexpected fixture request" } });
  });
  return { requests, unexpected };
}

async function expectContainedAssignment(page: Page) {
  await page.evaluate(() => document.fonts.ready);
  const failures = await page.evaluate(() => {
    const errors: string[] = [];
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const dialogBox = dialog.getBoundingClientRect();
    if (dialogBox.left < -1 || dialogBox.right > innerWidth + 1) errors.push("Dialog exceeds viewport width");
    for (const section of document.querySelectorAll<HTMLElement>('[data-testid="give-to-agent"], [data-testid="assignment-summary"]')) {
      const bounds = section.getBoundingClientRect();
      if (bounds.left < dialogBox.left - 1 || bounds.right > dialogBox.right + 1) errors.push(`${section.dataset.testid} exceeds dialog width`);
      const elements = [...section.querySelectorAll<HTMLElement>("button, input, select, label, p, span, code, strong, summary")]
        .filter((element) => element.getClientRects().length > 0);
      for (const element of elements) {
        const box = element.getBoundingClientRect();
        if (box.left < bounds.left - 1 || box.right > bounds.right + 1) {
          errors.push(`${element.getAttribute("aria-label") || element.textContent?.trim()} exceeds ${section.dataset.testid}`);
        }
      }
      // Inputs outside the tool list's scrollport are intentionally clipped.
      // Only their painted portion can overlap a neighboring control.
      const paintedBounds = (element: HTMLElement) => {
        const box = element.getBoundingClientRect();
        let top = box.top, bottom = box.bottom, left = box.left, right = box.right;
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
          if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
            top = Math.max(top, bounds.top);
            bottom = Math.min(bottom, bounds.bottom);
          }
          if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
            left = Math.max(left, bounds.left);
            right = Math.min(right, bounds.right);
          }
        }
        return { top, bottom, left, right };
      };
      const controls = elements.filter((element) => element.matches("button, input, select, summary"));
      for (let i = 0; i < controls.length; i++) for (let j = i + 1; j < controls.length; j++) {
        const a = paintedBounds(controls[i]), b = paintedBounds(controls[j]);
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
          errors.push(`${controls[i].getAttribute("aria-label") || controls[i].textContent} overlaps another control`);
        }
      }
    }
    if (document.documentElement.scrollWidth > innerWidth + 1) errors.push("Document overflows horizontally");
    return errors;
  });
  expect(failures).toEqual([]);
}

async function openAssignment(page: Page) {
  await page.goto("/assignment.html");
  await page.getByRole("button", { name: "Give to an agent…" }).click();
  await expect(page.getByTestId("give-to-agent")).toBeVisible();
  await page.getByRole("combobox", { name: "Agent", exact: true }).selectOption(assignmentAgentId);
  await expect(page.getByRole("option", { name: "Disabled agent" })).toHaveCount(0);
}

for (const width of [320, 390, 768, 1024, 1280, 1440]) {
  for (const selection of ["Select read-only", "Select all"]) {
    test(`assign Supabase ${selection} at ${width}px stays contained and pins grants to Bisby`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      const api = await mockAssignmentApi(page);
      await openAssignment(page);
      await expectContainedAssignment(page);
      await page.getByRole("button", { name: selection, exact: true }).click();
      const selected = assignmentTools.filter((tool) => selection === "Select all" || tool.risk === "read");
      await expect(page.getByRole("checkbox", { checked: true })).toHaveCount(selected.length);
      await expectContainedAssignment(page);
      await page.getByRole("button", { name: "Assign to agent", exact: true }).click();
      await expect(page.getByRole("status")).toContainText(`${selected.length} tool grants saved`);
      await expect(page.getByTestId("assignment-summary")).toContainText(`${selected.length} tools authorized on this app.`);
      await page.getByText("Advanced grant details", { exact: true }).click();
      await expectContainedAssignment(page);
      expect(api.requests).toEqual(selected.map((tool) => ({
        capability: tool.required_capability,
        scope: { connection_id: assignmentConnection.id },
        effect: "allow",
      })));
      expect(api.unexpected).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath("assignment.png"), fullPage: true });
    });
  }
}

test("partial assignment failure stays contained and retries only the failed grant", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 1000 });
  const failedCapability = assignmentTools[1].required_capability;
  const api = await mockAssignmentApi(page, failedCapability);
  await openAssignment(page);
  await page.getByRole("button", { name: "Select all", exact: true }).click();
  await page.getByRole("button", { name: "Assign to agent", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("2 tool grants saved; 1 failed.");
  await expectContainedAssignment(page);
  await page.getByRole("button", { name: "Retry failed assignments", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("3 tool grants saved.");
  await expect(page.getByTestId("assignment-summary")).toContainText("3 tools authorized on this app.");
  await expectContainedAssignment(page);
  expect(api.requests.map((request) => request.capability)).toEqual([
    ...assignmentTools.map((tool) => tool.required_capability), failedCapability,
  ]);
  expect(api.requests.every((request) => request.scope.connection_id === assignmentConnection.id)).toBe(true);
  expect(api.unexpected).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("assignment-retry.png"), fullPage: true });
});
