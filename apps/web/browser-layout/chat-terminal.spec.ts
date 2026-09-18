import { expect, test, type Page, type Route } from "@playwright/test";
import type { ConversationDetail, ConversationToolCall } from "../lib/types";

const base = "/api/v1/workspaces/browser-workspace";
const path = `${base}/conversations/browser-chat`;
const started = "2026-09-11T12:00:00Z";
const ended = "2026-09-11T12:01:00Z";
const command = "printf 'Installing dependencies\\n'; sleep 5; printf 'Build complete\\n'";

async function mockTerminal(page: Page) {
  let stdout = "";
  let completed = false;
  let hold = false;
  let held: { route: Route; json: unknown } | null = null;
  const unexpected: string[] = [];
  const call = (): ConversationToolCall => ({
    id: "terminal-call", agent_id: "browser-bisby", agent_name: "Bisby", task_id: "terminal-task", run_id: "terminal-run",
    tool_name: "cli.command.execute", sanitized_input_json: { input: { command } }, sanitized_output_json: {},
    status: completed ? "completed" : "executing", approval_id: null, started_at: started,
    completed_at: completed ? ended : null, duration_ms: completed ? 60000 : null, error_code: null, created_at: started,
    sandbox_job: stdout || completed ? {
      job_id: "terminal-job", status: completed ? "completed" : "running", network_policy: "none",
      stdout, stderr: completed ? "A harmless warning\n" : "", output_is_tail: true,
      exit_code: completed ? 0 : null, started_at: started, completed_at: completed ? ended : null,
      duration_ms: completed ? 60000 : null,
    } : null,
  });
  const detail = (): ConversationDetail => ({
    conversation: {
      id: "browser-chat", workspace_id: "browser-workspace", title: "Build the project", status: "active", pinned: false,
      primary_agent_id: "browser-bisby", created_by_user_id: "browser-user", created_at: started,
      updated_at: completed ? ended : started, last_activity_at: completed ? ended : started,
      active_task_id: completed ? null : "terminal-task", active_task_state: completed ? null : "running",
      active_run_status: completed ? null : "running", active_run_started_at: completed ? null : started,
      active_activity: completed ? null : "Running a command", last_message_preview: completed ? "The build finished." : "Build the project",
      last_message_sender_type: completed ? "agent" : "user", agent_name: "Bisby", agent_role_title: "Engineer", task_count: 1,
    },
    agent: { id: "browser-bisby", name: "Bisby", role_title: "Engineer", status: "active", availability: "available", public_purpose: "Help with software" },
    tasks: [], pending_approvals: [], total_input_tokens: 0, total_output_tokens: 0, total_cost_micros: 0,
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET") {
      if (url === `${path}/items`) return route.fulfill({ status: 404, json: { detail: "Journal disabled for polling fixture" } });
      if (url === `${path}/files`) return route.fulfill({ json: { items: [], has_more: false } });
      if (url === `${base}/connections`) return route.fulfill({ json: [] });
      if (url === path) return route.fulfill({ json: detail() });
      if (url === `${path}/tool-calls`) {
        const json = { items: [call()], has_more: false, limit: 100 };
        if (hold) { hold = false; held = { route, json }; return; }
        return route.fulfill({ json });
      }
      if (url === `${path}/messages`) return route.fulfill({ json: [{
        id: "terminal-request", task_id: "terminal-task", run_id: null, sender_type: "user", sender_id: "browser-user",
        message_type: "text", content_json: { text: "Build the project" }, created_at: started,
        conversation_id: "browser-chat", sender_name: "Dev Owner", agent_id: null,
      }, ...(completed ? [{
        id: "terminal-answer", task_id: "terminal-task", run_id: "terminal-run", sender_type: "agent", sender_id: "browser-bisby",
        message_type: "text", content_json: { text: "The build finished." }, created_at: ended,
        conversation_id: "browser-chat", sender_name: "Bisby", agent_id: "browser-bisby",
      }] : [])] });
      if (url === `${path}/activity`) return route.fulfill({ json: { items: [], next_before: null } });
      if (url === `${base}/agents`) return route.fulfill({ json: [{ id: "browser-bisby", name: "Bisby", status: "active" }] });
      if (url === `${base}/agents/browser-bisby`) return route.fulfill({ json: { id: "browser-bisby", name: "Bisby", status: "active", model_profile_id: null } });
      if (url === `${base}/agents/browser-bisby/policy`) return route.fulfill({ json: { rules: [], preset: "balanced", autonomy_level: "supervised" } });
      if ([`${base}/model-profiles`, `${base}/tools`, `${base}/agents/browser-bisby/grants`, `${base}/agents/browser-bisby/bundles`].includes(url)) return route.fulfill({ json: [] });
    }
    unexpected.push(`${route.request().method()} ${url}`);
    return route.fulfill({ status: 500, json: { detail: "Unexpected fixture request" } });
  });
  return {
    unexpected,
    output: (value: string) => { stdout = value; },
    holdNext: () => { hold = true; },
    hasHeld: () => held !== null,
    complete: () => { completed = true; },
    release: async () => { if (held) { await held.route.fulfill({ json: held.json }); held = null; } },
  };
}

for (const width of [390, 1440]) {
  test(`actual terminal command, changing output, completion and reload history at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const api = await mockTerminal(page);
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/chat.html");
    const card = page.getByTestId("terminal-call");
    await expect(card).toHaveCount(1);
    await expect(card.getByText(command, { exact: true })).toBeVisible();
    await expect(card.getByText("Starting", { exact: true })).toBeVisible();
    api.output("Installing dependencies\n");
    await expect(card.getByLabel("Standard output")).toHaveText("Installing dependencies\n");
    await expect(card.getByText("Running", { exact: true })).toBeVisible();
    api.output("Installing dependencies\nBuild complete\n");
    await expect(card.getByLabel("Standard output")).toHaveText("Installing dependencies\nBuild complete\n");
    await page.screenshot({ path: testInfo.outputPath("terminal-running.png"), fullPage: true });

    api.holdNext();
    await expect.poll(api.hasHeld).toBe(true);
    api.complete();
    await expect(page.getByText("The build finished.", { exact: true })).toBeVisible();
    await api.release();
    await expect(card.getByText("Completed", { exact: true })).toBeVisible();
    await expect(card.getByText("Exit 0", { exact: true })).toBeVisible();
    await expect(card.getByLabel("Standard error")).toHaveText("A harmless warning\n");
    await expect(card.getByLabel("Standard output")).toHaveText("Installing dependencies\nBuild complete\n");

    await page.reload();
    await expect(card).toHaveCount(1);
    await expect(card.getByText(command, { exact: true })).toBeVisible();
    await expect(card.getByText("Exit 0", { exact: true })).toBeVisible();
    await expect(card.getByLabel("Standard output")).toHaveText("Installing dependencies\nBuild complete\n");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(api.unexpected).toEqual([]);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("terminal-completed.png"), fullPage: true });
  });
}
