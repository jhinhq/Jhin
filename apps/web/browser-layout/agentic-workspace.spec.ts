import { expect, test, type Page } from "@playwright/test";
import type { ManagedFile } from "../lib/workspace-files";
const base = "/api/v1/workspaces/browser-workspace", chat = `${base}/conversations/browser-chat`;
const time = "2026-09-11T12:00:00Z";
const source = "name,total\nNorth,42\nSouth,18\n";
const file: ManagedFile = { id: "report", workspace_id: "browser-workspace", conversation_id: "browser-chat", name: "sales.csv", path: "sales.csv", kind: "artifact", status: "ready", error: null, mime_type: "text/csv", size_bytes: source.length, preview_kind: "text", current_revision_id: "revision-1", version: 1, sha256: "sha1", extracted_text: source, extraction_truncated: false, created_at: time, updated_at: time, download_url: `${base}/files/report/download`, preview_url: `${base}/files/report/preview` };
async function setup(page: Page) {
  let owner = "agent", generation = 1, content = source;
  let current = { ...file };
  const sent: Record<string, unknown>[] = [], writes: Record<string, unknown>[] = [], files: ManagedFile[] = [current];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    if (path === `${chat}/items`) return route.fulfill({ status: 404, json: { detail: "Polling fixture" } });
    if (path === chat) return route.fulfill({ json: {
      conversation: { id: "browser-chat", workspace_id: "browser-workspace", title: "Analyze sales", status: "active", pinned: false, primary_agent_id: "bisby", created_by_user_id: "browser-user", created_at: time, updated_at: time, last_activity_at: time, active_task_id: null, active_task_state: null, active_run_status: null, active_run_started_at: null, active_activity: null, last_message_preview: "Here is the report.", last_message_sender_type: "agent", agent_name: "Bisby", agent_role_title: "Analyst", task_count: 1 },
      agent: { id: "bisby", name: "Bisby", role_title: "Analyst", status: "active", availability: "available", public_purpose: "Analyze files" }, tasks: [], pending_approvals: [], total_input_tokens: 0, total_output_tokens: 0, total_cost_micros: 0,
    } });
    if (path === `${chat}/messages`) return route.fulfill({ json: [{ id: "answer", conversation_id: "browser-chat", task_id: "task", run_id: "run", sender_type: "agent", sender_id: "bisby", sender_name: "Bisby", agent_id: "bisby", message_type: "text", content_json: { text: "## Sales report\n\n| Region | Total |\n| --- | --- |\n| North | 42 |\n| South | 18 |\n\n- [x] Analyze the spreadsheet\n- [x] Create an editable report\n\n```python\ntotal = 42 + 18\nprint(total)\n```" }, created_at: time }] });
    if (path === `${chat}/activity`) return route.fulfill({ json: { items: [], next_before: null } });
    if (path === `${chat}/tool-calls`) return route.fulfill({ json: { items: [{ id: "app-call", tool_name: "supabase.list_projects", agent_name: "Bisby", status: "executed", sanitized_input_json: {}, sanitized_output_json: { summary: "Found 2 projects" }, created_at: time, sandbox_job: null }], limit: 100, has_more: false } });
    if (path === `${chat}/files` && method === "GET") return route.fulfill({ json: { items: files, has_more: false } });
    if (path === `${chat}/files` && method === "POST") { const uploaded = { ...file, id: "input", name: "input.csv", kind: "upload" as const }; files.push(uploaded); return route.fulfill({ status: 201, json: uploaded }); }
    if (path === `${chat}/turns`) { const body = request.postDataJSON(); sent.push(body); return route.fulfill({ status: 201, json: { message: { id: "user", task_id: "task", run_id: null, sender_type: "user", sender_id: "browser-user", message_type: "text", content_json: body, created_at: time, conversation_id: "browser-chat", sender_name: "Dev Owner", agent_id: null }, task_id: "task", mode: "new_task" } }); }
    if (path === `${chat}/runtime`) return route.fulfill({ json: { workspace_key: "conversation-browser-chat", cwd: "/workspace", owner, owner_user_id: owner === "user" ? "browser-user" : null, lease_generation: generation, terminal: null, previews: [] } });
    if (path === `${chat}/runtime/control`) { owner = request.postDataJSON().action === "take" ? "user" : "agent"; generation++; return route.fulfill({ json: { owner, lease_generation: generation } }); }
    if (path === `${base}/files/report/versions`) return route.fulfill({ json: { items: [{ id: current.current_revision_id, version: current.version }, ...(current.version > 1 ? [{ id: "revision-1", version: 1 }] : [])] } });
    if (path === `${base}/files/report/content` && method === "GET") return route.fulfill({ json: { file_id: "report", revision_id: current.current_revision_id, path: "sales.csv", content, truncated: false, editable: true, sha256: current.sha256 } });
    if (path === `${base}/files/report/content` && method === "PUT") { const body = request.postDataJSON(); writes.push(body); if (body.expected_revision_id !== current.current_revision_id || body.lease_generation !== generation || owner !== "user") return route.fulfill({ status: 409, json: { detail: "File or lease changed" } }); content = body.content; current = { ...current, current_revision_id: "revision-2", version: 2, sha256: "sha2" }; files[0] = current; return route.fulfill({ json: current }); }
    if (path === `${base}/files/report/annotations`) return route.fulfill({ json: { id: "annotation", ...request.postDataJSON() } });
    if (path === `${chat}/changes`) return route.fulfill({ json: { items: [{ path: "sales.csv", status: "modified", current_sha256: current.sha256, before_revision_id: "revision-1", after_revision_id: current.current_revision_id, diff: "--- sales.csv\n+++ sales.csv\n-North,42\n+North,50" }], excluded: [] } });
    if (path === `${chat}/checkpoints`) return route.fulfill({ json: { items: [{ id: "checkpoint", label: "Before editing", manifest_json: { "sales.csv": "revision-1" }, excluded_json: [], created_at: time }] } });
    if (path === `${base}/agents`) return route.fulfill({ json: [{ id: "bisby", name: "Bisby", status: "active" }] });
    if ([`${base}/connections`, `${base}/projects`, `${base}/model-profiles`].includes(path)) return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: `Unhandled fixture ${method} ${path}` } });
  });
  return { sent, writes, errors, files };
}

test("terminal authority, input, retained output and opaque interactive preview", async ({ page }, testInfo) => {
  const fixture = await setup(page);
  fixture.files.push({ ...file, id: "html", name: "index.html", path: "index.html", mime_type: "text/html", preview_kind: "code", download_url: `${base}/files/html/download` });
  let owner = "agent", lease = 1;
  let terminal: Record<string, unknown> | null = null;
  const input: string[] = [];
  const preview = { id: "preview", kind: "preview", status: "running", cwd: "/workspace", network: "none", output: "Preview ready", output_offset: 13, lease_generation: 2, created_at: time, revision_id: "revision-1", exit_code: null };
  await page.routeWebSocket("**/runtime/sessions/terminal/ws**", (socket) => {
    socket.send(JSON.stringify({ type: "output", data: "$ ", offset: 2 }));
    socket.onMessage((message) => {
      const data = JSON.parse(String(message));
      if (data.type === "input") { input.push(data.data); socket.send(JSON.stringify({ type: "ack", seq: data.seq })); if (data.data === "\r") socket.send(JSON.stringify({ type: "output", data: "\r\nfile.txt\r\n$ ", offset: 17 })); }
    });
  });
  await page.route("**/runtime/previews/**", (route) => route.fulfill({ contentType: "text/html", headers: { "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-downloads" }, body: '<!doctype html><button id="count">Increment</button><output id="value">0</output><button id="scope">Check isolation</button><script>let n=0;count.onclick=()=>value.textContent=++n;scope.onclick=()=>{try{parent.document.body.innerHTML="bad"}catch{scope.textContent="Isolated"}}</script>' }));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `${chat}/runtime`) return route.fulfill({ json: { workspace_key: "chat", cwd: "/workspace", owner, owner_user_id: owner === "user" ? "browser-user" : null, lease_generation: lease, terminal, previews: [] } });
    if (path === `${chat}/runtime/control`) { owner = route.request().postDataJSON().action === "take" ? "user" : "agent"; lease++; return route.fulfill({ json: {} }); }
    if (path === `${chat}/terminals`) { terminal = { ...preview, id: "terminal", kind: "terminal", user_id: "browser-user", output: "", output_offset: 0 }; return route.fulfill({ status: 201, json: terminal }); }
    if (path === `${chat}/terminals/terminal/ticket`) return route.fulfill({ json: { ticket: "fixture-terminal-ticket", websocket_url: "/runtime/sessions/terminal/ws" } });
    if (path === `${chat}/terminals/terminal/close`) { terminal = { ...terminal, status: "closed", output: "$ ls\r\nfile.txt\r\n$ ", output_offset: 20, exit_code: 0 }; return route.fulfill({ json: terminal }); }
    if (path === `${chat}/previews`) return route.fulfill({ status: 201, json: preview });
    if (path === `${chat}/previews/preview/ticket`) return route.fulfill({ json: { url: "/runtime/previews/preview/fixture-preview-ticket/" } });
    if (path === `${chat}/previews/preview/refresh`) return route.fulfill({ json: { ...preview, revision_id: "revision-2" } });
    return route.fallback();
  });
  await page.goto("/chat.html?role=admin"); await page.getByRole("button", { name: /Files & workspace/ }).click();
  await page.getByRole("tab", { name: "terminal", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open terminal" })).toHaveCount(0);
  await page.getByLabel("Chat workspace").getByRole("button", { name: "Take control", exact: true }).first().click();
  await page.getByRole("button", { name: "Open terminal", exact: true }).click();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await page.locator(".xterm-helper-textarea").focus(); await page.keyboard.type("ls"); await page.keyboard.press("Enter");
  await expect.poll(() => input.join("")).toBe("ls\r");
  await page.getByRole("button", { name: "Close terminal", exact: true }).click();
  await expect(page.getByText("closed · Exit 0")).toBeVisible(); await expect(page.getByRole("button", { name: "New terminal", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("terminal-retained.png"), fullPage: true });
  await page.getByRole("tab", { name: "preview", exact: true }).click();
  await page.getByRole("combobox", { name: "Preview source" }).selectOption("html"); await page.getByRole("button", { name: "Open preview", exact: true }).click();
  const frame = page.frameLocator('iframe[title="App preview"]'); await frame.getByRole("button", { name: "Increment" }).click(); await expect(frame.locator("output")).toHaveText("1");
  await frame.getByRole("button", { name: "Check isolation" }).click(); await expect(frame.getByRole("button", { name: "Isolated" })).toBeVisible();
  await expect(page.locator('iframe[title="App preview"]')).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-downloads");
  await page.getByRole("button", { name: "Refresh from changes" }).click(); await expect(page.getByText("Revision revision-2")).toBeVisible();
  expect(fixture.errors).toEqual([]); await page.screenshot({ path: testInfo.outputPath("interactive-preview.png"), fullPage: true });
});
for (const width of [390, 1440]) {
  test(`files, formatted results, revision editing and context round trip at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 }); const fixture = await setup(page);
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/chat.html?role=admin");
    await expect(page.getByRole("table")).toBeVisible(); await expect(page.getByText("Found 2 projects", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Copy code" }).click(); await expect(page.getByRole("button", { name: "Copied", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Use in next message", exact: true }).click();
    await expect(page.getByRole("list", { name: "Message attachments" })).toContainText("sales.csv");
    await page.getByRole("textbox", { name: "Message", exact: true }).fill("Revise this report.");
    await page.reload(); await expect(page.getByRole("textbox", { name: "Message", exact: true })).toHaveValue("Revise this report."); await expect(page.getByRole("list", { name: "Message attachments" })).toContainText("sales.csv");
    await page.getByRole("button", { name: /Files & workspace/ }).click();
    await page.getByRole("button", { name: /sales.csv.*artifact/ }).click();
    await expect(page.getByRole("button", { name: "Save changes" })).toHaveCount(0);
    await page.getByRole("button", { name: "Take control", exact: true }).click();
    await expect(page.getByRole("button", { name: "Return to agent", exact: true })).toBeVisible();
    await expect(page.locator(".cm-content")).toBeVisible(); await page.locator(".cm-content").fill("name,total\nNorth,50\nSouth,18\n");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Version" })).toHaveValue("revision-2");
    expect(fixture.writes[0]).toMatchObject({ expected_revision_id: "revision-1", lease_generation: 2 });
    await page.screenshot({ path: testInfo.outputPath("workspace-editor.png"), fullPage: true });
    await page.getByRole("tab", { name: "changes", exact: true }).click(); await expect(page.getByRole("checkbox", { name: "Select sales.csv" })).toBeVisible();
    await page.getByRole("button", { name: "Close workspace" }).click();
    await page.locator('input[type="file"]').setInputFiles({ name: "input.csv", mimeType: "text/csv", buffer: Buffer.from(source) });
    await expect(page.getByRole("list", { name: "Message attachments" })).toContainText("input.csv");
    await page.getByRole("combobox", { name: "This turn's mode", exact: true }).selectOption("plan");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await expect.poll(() => fixture.sent.length).toBe(1);
    expect(fixture.sent[0]).toMatchObject({ execution_mode: "plan", attachment_ids: ["report", "input"], context_refs: [{ type: "file", id: "report", revision_id: "revision-1", label: "sales.csv" }, { type: "file", id: "input", revision_id: "revision-1", label: "input.csv" }] });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(fixture.errors).toEqual([]); await page.screenshot({ path: testInfo.outputPath("workspace-chat.png"), fullPage: true });
  });
}

test("journal history stays paginated and recoverable without fetching the unbounded legacy transcript", async ({ page }) => {
  await setup(page);
  let legacyRequests = 0;
  const recent = { id: "message:journal", version: 1, sequence: 20, revision: 20, kind: "message", status: "completed", actor: { type: "agent", id: "bisby", name: "Bisby" }, task_id: "task", run_id: "run", created_at: time, data: { id: "journal", sender_type: "agent", sender_id: "bisby", message_type: "text", content_json: { text: "Journal response is authoritative." }, created_at: time } };
  const earlier = { ...recent, id: "message:older", sequence: 10, revision: 10, created_at: "2026-09-10T12:00:00Z", data: { ...recent.data, id: "older", created_at: "2026-09-10T12:00:00Z", content_json: { text: "Earlier retained response." } } };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `${chat}/messages`) { legacyRequests++; return route.fulfill({ json: [] }); }
    if (url.pathname === `${chat}/items`) return route.fulfill({ json: { items: url.searchParams.has("before") ? [earlier] : [recent], cursor: 20, next_before: url.searchParams.has("before") ? null : 10, has_more: !url.searchParams.has("before"), version: 1 } });
    if (url.pathname === `${chat}/events`) return route.fulfill({ contentType: "text/event-stream", body: `event: item\nid: 20\ndata: ${JSON.stringify(recent)}\n\n` });
    return route.fallback();
  });
  await page.goto("/chat.html?role=admin");
  await expect(page.getByText("Journal response is authoritative.")).toHaveCount(1);
  await page.getByRole("button", { name: "Load earlier activity" }).click();
  await expect(page.getByText("Earlier retained response.")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Load earlier activity" })).toHaveCount(0);
  await expect(page.getByText(/Reconnecting/)).toBeVisible();
  expect(legacyRequests).toBe(0);
});

test("an unavailable event transport keeps bounded journal polling without the legacy transcript", async ({ page }) => {
  await setup(page);
  let legacyRequests = 0, snapshots = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `${chat}/messages`) { legacyRequests++; return route.fulfill({ json: [] }); }
    if (url.pathname === `${chat}/items`) { snapshots++; return route.fulfill({ json: { items: [], cursor: 0, next_before: null, has_more: false, version: 1 } }); }
    if (url.pathname === `${chat}/events`) return route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } });
    return route.fallback();
  });
  await page.goto("/chat.html?role=admin");
  await expect(page.getByText("Checking for updates", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => snapshots, { timeout: 20_000 }).toBeGreaterThan(1);
  expect(legacyRequests).toBe(0);
});
