import { expect, test, type Page, type Route } from "@playwright/test";
import type { ActivityCard, Conversation, ConversationDetail, ConversationMessage } from "../lib/types";

const workspaceId = "browser-workspace";
const conversationId = "browser-chat";
const agentId = "browser-bisby";
const question = "What about now?";
const answer = "Yes, I can access the connected Supabase app now.";
const base = `/api/v1/workspaces/${workspaceId}`;
const chatPath = `${base}/conversations/${conversationId}`;
const initialTime = "2026-09-10T12:00:00.000Z";
const sentTime = "2026-09-10T12:01:00.000Z";
const completedTime = "2026-09-10T12:02:00.000Z";

function message(id: string, sender: "user" | "agent", text: string, createdAt: string): ConversationMessage {
  return {
    id, task_id: "browser-task", run_id: sender === "agent" ? "browser-run" : null,
    sender_type: sender, sender_id: sender === "agent" ? agentId : "browser-user",
    message_type: "text", content_json: { text }, created_at: createdAt,
    conversation_id: conversationId, sender_name: sender === "agent" ? "Bisby" : "Dev Owner",
    agent_id: sender === "agent" ? agentId : null,
  };
}

async function mockChatApi(page: Page) {
  let phase: "idle" | "running" | "completed" = "idle";
  const unexpected: string[] = [];
  const sends: Array<{ text: string; client_turn_id: string }> = [];
  const messages: ConversationMessage[] = [message("earlier-reply", "agent", "I could not access Supabase earlier.", initialTime)];
  let activity: ActivityCard[] = [];
  let holdNextMessages = false;
  let holdNextActivity = false;
  const held = new Map<string, { route: Route; json: unknown }>();
  const counts = { messages: 0, activity: 0, detail: 0 };

  const conversation = (): Conversation => ({
    id: conversationId, workspace_id: workspaceId, title: "Supabase access", status: "active", pinned: false,
    primary_agent_id: agentId, created_by_user_id: "browser-user", created_at: initialTime,
    updated_at: phase === "idle" ? initialTime : phase === "running" ? sentTime : completedTime,
    last_activity_at: phase === "idle" ? initialTime : phase === "running" ? sentTime : completedTime,
    active_task_id: phase === "running" ? "browser-task" : null,
    active_task_state: phase === "running" ? "running" : null,
    active_run_status: phase === "running" ? "running" : null,
    active_run_started_at: phase === "running" ? sentTime : null,
    active_activity: null, last_message_preview: phase === "completed" ? answer : phase === "running" ? question : "I could not access Supabase earlier.",
    last_message_sender_type: phase === "running" ? "user" : "agent",
    agent_name: "Bisby", agent_role_title: "Software Engineer", task_count: phase === "idle" ? 1 : 2,
  });
  const detail = (): ConversationDetail => ({
    conversation: conversation(), tasks: [], pending_approvals: [],
    total_input_tokens: 0, total_output_tokens: 0, total_cost_micros: 0,
    agent: { id: agentId, name: "Bisby", role_title: "Software Engineer", status: "active", availability: "available", public_purpose: "Help with software" },
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === `${chatPath}/turns`) {
      const body = request.postDataJSON() as { text: string; client_turn_id: string };
      sends.push(body);
      phase = "running";
      const sent = message("current-question", "user", body.text, sentTime);
      sent.content_json.client_turn_id = body.client_turn_id;
      messages.push(sent);
      return route.fulfill({ status: 201, json: { conversation: conversation(), message: sent, task_id: "browser-task", mode: "new_task" } });
    }
    if (request.method() === "GET") {
      if (path === `${chatPath}/items`) return route.fulfill({ status: 404, json: { detail: "Journal disabled for polling fixture" } });
      if (path === `${chatPath}/files`) return route.fulfill({ json: { items: [], has_more: false } });
      if (path === `${base}/connections`) return route.fulfill({ json: [] });
      if (path === `${chatPath}/tool-calls`) return route.fulfill({ json: { items: [], has_more: false, limit: 100 } });
      if (path === chatPath) {
        counts.detail++;
        return route.fulfill({ json: detail() });
      }
      if (path === `${chatPath}/messages`) {
        counts.messages++;
        const json = structuredClone(messages);
        if (holdNextMessages) {
          holdNextMessages = false;
          held.set("messages", { route, json });
          return;
        }
        return route.fulfill({ json });
      }
      if (path === `${chatPath}/activity`) {
        counts.activity++;
        const json = { items: structuredClone(activity), next_before: null };
        if (holdNextActivity) {
          holdNextActivity = false;
          held.set("activity", { route, json });
          return;
        }
        return route.fulfill({ json });
      }
      if (path === `${base}/agents`) return route.fulfill({ json: [{ id: agentId, name: "Bisby", status: "active" }] });
      if (path === `${base}/agents/${agentId}`) return route.fulfill({ json: { id: agentId, name: "Bisby", status: "active", model_profile_id: null } });
      if (path === `${base}/agents/${agentId}/policy`) return route.fulfill({ json: { rules: [], preset: "balanced", autonomy_level: "supervised" } });
      if ([`${base}/model-profiles`, `${base}/tools`, `${base}/agents/${agentId}/grants`, `${base}/agents/${agentId}/bundles`].includes(path)) {
        return route.fulfill({ json: [] });
      }
    }
    unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 500, json: { detail: "Unexpected fixture request" } });
  });

  return {
    sends, unexpected, counts,
    heldCount: () => held.size,
    holdNextTranscriptPoll: () => { holdNextMessages = true; holdNextActivity = true; },
    complete: () => {
      phase = "completed";
      messages.push(message("current-reply", "agent", answer, completedTime));
      activity = [{
        id: "browser-finished", kind: "finished", label: "Finished", summary: "Supabase access verified",
        actor_type: "agent", actor_agent_id: agentId, actor_agent_name: "Bisby", target_agent_id: null,
        target_agent_name: null, task_id: "browser-task", task_title: "Check access", root_task_id: "browser-task",
        conversation_id: conversationId, approval_id: null, detail_json: {}, created_at: completedTime,
      }];
    },
    releaseStalePolls: async () => {
      for (const { route, json } of held.values()) await route.fulfill({ json });
      held.clear();
    },
  };
}

for (const width of [390, 1440]) {
  test(`completed reply appears without reload after an old in-flight poll at ${width}px`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width, height: 900 });
    const api = await mockChatApi(page);
    const pageErrors: string[] = [];
    let navigations = 0;
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) navigations++; });
    await page.goto("/chat.html");
    await expect(page.getByText("I could not access Supabase earlier.", { exact: true })).toBeVisible();
    await page.getByRole("combobox", { name: "Activity detail", exact: true }).selectOption("detailed");
    await page.getByRole("textbox", { name: "Message", exact: true }).fill(question);
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await expect(page.getByTestId("working-indicator")).toBeVisible();

    // Deliberately exceed the production 20-second post-send window in real
    // browser time. Do not replace the page's polling hooks with test timers.
    await page.waitForTimeout(20_500);
    api.holdNextTranscriptPoll();
    await expect.poll(api.heldCount).toBe(2);
    const heldCounts = { ...api.counts };
    api.complete();
    await expect(page.getByTestId("working-indicator")).toHaveCount(0);
    await api.releaseStalePolls();

    await expect(page.getByTestId("agent-message").filter({ hasText: answer })).toBeVisible();
    await expect(page.getByText("Finished", { exact: true })).toBeVisible();
    expect(api.counts.messages).toBeGreaterThan(heldCounts.messages);
    expect(api.counts.activity).toBeGreaterThan(heldCounts.activity);
    expect(api.sends).toHaveLength(1);
    expect(api.sends[0]).toMatchObject({ text: question, client_turn_id: expect.any(String), execution_mode: "act" });
    expect(navigations).toBe(1);
    expect(api.unexpected).toEqual([]);
    expect(pageErrors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("chat-completed.png"), fullPage: true });
  });
}
