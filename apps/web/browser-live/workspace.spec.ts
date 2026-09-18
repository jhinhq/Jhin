/** Explicit opt-in only. Real authenticated APIs, containers and transport; no route mocks. */
import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
const workspaceId = process.env.JHIN_LIVE_WORKSPACE_ID;
const agentId = process.env.JHIN_LIVE_AGENT_ID;
test.skip(process.env.JHIN_LIVE_WORKSPACE_ACCEPTANCE !== "1" || !workspaceId || !agentId || !process.env.JHIN_LIVE_AUTH_STATE, "Requires explicit live acceptance and an existing owner/admin browser session.");
const base = `/api/v1/workspaces/${workspaceId}`;
const createdChats: string[] = [];

test.beforeEach(async ({ page }) => {
  const sanitize = (text: string) => text.replace(/(\/runtime\/previews\/[^/]+\/)[^/?#\s'"<>]+/g, "$1[ticket]");
  const logged = new Set<string>();
  const log = (message: string) => { if (!logged.has(message)) { logged.add(message); console.info(message); } };
  page.on("console", (message) => { if (message.type() === "error") log(`Browser console: ${sanitize(message.text())}`); });
  page.on("pageerror", (error) => log(`Browser error: ${sanitize(error.message)}`));
  page.on("websocket", (socket) => socket.on("socketerror", (error) => log(`Browser socket: ${sanitize(error)}`)));
  page.on("requestfailed", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/runtime/")) log(`Browser runtime request failed: ${request.method()} ${request.failure()?.errorText}`);
  });
  page.on("response", async (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/api/") && url.pathname.endsWith("/previews") && response.request().method() === "POST" && response.ok()) {
      const session = await response.json(); console.info(`Browser acceptance: preview session ${session.id}`);
    }
    if (url.pathname.startsWith("/runtime/") && response.status() >= 400) log(`Browser runtime response: ${response.request().method()} ${response.status()}`);
  });
});

test.afterEach(async ({ page }) => {
  // Stop only sessions created by this test; retain its chat/files as evidence.
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "jhin_csrf")?.value;
  for (const path of createdChats.splice(0)) {
    try {
      const response = await page.request.get(`${path}/runtime`, { timeout: 10_000 });
      if (!response.ok()) continue;
      const runtime = await response.json();
      const stop = (suffix: string, data?: unknown) => page.request.post(`${path}${suffix}`, { data, timeout: 10_000, headers: csrf ? { "x-csrf-token": csrf } : {} });
      if (runtime.terminal && ["starting", "running", "stopping"].includes(runtime.terminal.status)) await stop(`/terminals/${runtime.terminal.id}/close`);
      for (const preview of runtime.previews ?? []) if (["starting", "running", "stopping"].includes(preview.status)) await stop(`/previews/${preview.id}/stop`);
      if (runtime.owner === "user") await stop("/runtime/control", { action: "return" });
    } catch { /* The acceptance result records transport failure; server TTL still bounds sessions. */ }
  }
});

async function post(page: Page, path: string, data?: unknown) {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "jhin_csrf")?.value;
  const response = await page.request.post(path, { data, headers: csrf ? { "x-csrf-token": csrf } : {} });
  expect(response.ok(), `POST ${path} returned ${response.status()}`).toBe(true);
  return response.json();
}
async function start(page: Page) {
  const result = await post(page, `${base}/conversations`, { agent_id: agentId, title: `Workspace acceptance ${new Date().toISOString()}` });
  const id = result.conversation.id as string;
  createdChats.push(`${base}/conversations/${id}`);
  await page.goto(`/chats/${id}`); await expect(page.getByRole("button", { name: /Files & workspace/ })).toBeVisible();
  return { id, path: `${base}/conversations/${id}` };
}
async function upload(page: Page, name: string, mimeType: string, source: string) {
  await page.locator('input[type="file"]').setInputFiles({ name, mimeType, buffer: Buffer.from(source) });
  await expect(page.getByRole("list", { name: "Message attachments" })).toContainText(name, { timeout: 60_000 });
}

async function installSources(page: Page, chatPath: string, sources: Record<string, string>) {
  const runtime = await post(page, `${chatPath}/runtime/control`, { action: "take" });
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "jhin_csrf")?.value;
  const headers: Record<string, string> = csrf ? { "x-csrf-token": csrf } : {};
  const files: Record<string, { id: string; current_revision_id: string }> = {};
  for (const [path, content] of Object.entries(sources)) {
    const upload = await page.request.post(`${chatPath}/files`, { headers, multipart: { path, file: { name: path.split("/").at(-1)!, mimeType: "text/plain", buffer: Buffer.from(content) } } });
    expect(upload.ok(), `Upload ${path}: ${upload.status()}`).toBe(true);
    const file = await upload.json();
    const save = await page.request.put(`${base}/files/${file.id}/content`, { headers, data: { content, expected_revision_id: file.current_revision_id, lease_generation: runtime.lease_generation } });
    expect(save.ok(), `Materialize ${path}: ${save.status()}`).toBe(true);
    files[path] = await save.json();
  }
  return files;
}

async function reviseSource(page: Page, chatPath: string, file: { id: string; current_revision_id: string }, content: string) {
  const runtime = await (await page.request.get(`${chatPath}/runtime`)).json();
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "jhin_csrf")?.value;
  const response = await page.request.put(`${base}/files/${file.id}/content`, { headers: csrf ? { "x-csrf-token": csrf } : {}, data: { content, expected_revision_id: file.current_revision_id, lease_generation: runtime.lease_generation } });
  expect(response.ok(), `Source revision returned ${response.status()}`).toBe(true);
  return response.json();
}

const echoHtml = `<!doctype html><h1>HTTP browser preview</h1><button id="post">Send POST</button><button id="socket">Send WebSocket</button><pre id="result"></pre><script>
post.onclick=async()=>{try{const r=await fetch(new URL('echo',location.href),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hello:'browser'})});result.textContent=JSON.stringify(await r.json())}catch(e){result.textContent=String(e)}};
socket.onclick=()=>{const u=new URL('socket',location.href);u.protocol=location.protocol==='https:'?'wss:':'ws:';const w=new WebSocket(u);w.onopen=()=>w.send('browser-ping');w.onmessage=e=>{result.textContent=e.data;w.close()};w.onerror=()=>result.textContent='WebSocket failed'};
</script>`;
const echoServer = `import base64,hashlib,http.server,json,struct
HTML=${JSON.stringify(echoHtml)}.encode()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Upgrade','').lower()=='websocket':
            self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
            first=self.rfile.read(2);n=first[1]&127
            if n==126:n=struct.unpack('!H',self.rfile.read(2))[0]
            mask=self.rfile.read(4);data=self.rfile.read(n);data=bytes(b^mask[i%4] for i,b in enumerate(data));result=b'echo:'+data;self.wfile.write(bytes([129,len(result)])+result);self.wfile.flush();return
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(HTML)
    def do_HEAD(self):
        self.send_response(200);self.end_headers()
    def do_POST(self):
        data=self.rfile.read(int(self.headers.get('Content-Length','0')));self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'received':json.loads(data),'cookie':self.headers.get('Cookie'),'authorization':self.headers.get('Authorization')}).encode())
http.server.ThreadingHTTPServer(('127.0.0.1',3000),Handler).serve_forever()
`;
const frameworks = [
  { framework: "vite", source: "package.json", heading: "Vite browser preview", files: {
    "package.json": JSON.stringify({ type: "module", dependencies: { vite: "8.2.1", react: "19.2.8", "react-dom": "19.2.8" } }),
    "index.html": '<!doctype html><div id="root"></div><script type="module" src="/main.js"></script>',
    "main.js": "import React,{useState} from 'react';import{createRoot}from'react-dom/client';function App(){const[n,set]=useState(0);return React.createElement('main',null,React.createElement('h1',null,'Vite browser preview'),React.createElement('button',{onClick:()=>set(n+1)},'Count '+n))}createRoot(document.getElementById('root')).render(React.createElement(App));",
  } },
  { framework: "next", source: "package.json", heading: "Next browser preview", files: {
    "package.json": JSON.stringify({ dependencies: { next: "16.3.1", react: "19.2.8", "react-dom": "19.2.8" } }),
    "app/layout.js": "export default function Layout({children}){return <html><body>{children}</body></html>}",
    "app/page.js": "'use client';import{useState,useEffect}from'react';export default function Page(){const[n,set]=useState(0);const[result,show]=useState('');useEffect(()=>{document.body.dataset.hydrated='yes'},[]);return <main><h1>Next browser preview</h1><button onClick={()=>set(n+1)}>Count {n}</button><button onClick={async()=>{try{const r=await fetch(location.pathname.replace(/\\/?$/,'/')+'api/echo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hello:'next-browser'})});show(JSON.stringify(await r.json()))}catch(e){show(String(e))}}}>Send POST</button><output>{result}</output></main>}",
    "app/api/echo/route.js": "export async function POST(req){return Response.json({received:await req.json()})}",
  } },
  { framework: "http", source: "server.py", heading: "HTTP browser preview", command: "python -I server.py", files: { "server.py": echoServer } },
] as const;

for (const fixture of frameworks) test(`real ${fixture.framework} hydration and scoped browser transport`, async ({ page }, info) => {
  test.setTimeout(300_000);
  let developmentFrames = 0;
  page.on("websocket", (socket) => {
    if (new URL(socket.url()).pathname.startsWith("/runtime/previews/")) socket.on("framereceived", () => { developmentFrames++; });
  });
  const chat = await start(page);
  const files = await installSources(page, chat.path, fixture.files);
  console.info(`Browser acceptance: ${fixture.framework} source files saved`);
  await page.reload(); await page.getByRole("button", { name: /Files & workspace/ }).click();
  await page.getByRole("tab", { name: "preview", exact: true }).click();
  await page.getByRole("combobox", { name: "Preview source" }).selectOption(files[fixture.source].id);
  await page.getByRole("combobox", { name: "Preview framework" }).selectOption(fixture.framework);
  if ("command" in fixture) await page.getByRole("textbox", { name: "Development command" }).fill(fixture.command);
  await page.getByRole("button", { name: "Open preview", exact: true }).click();
  const frame = page.frameLocator('iframe[title="App preview"]');
  await expect(frame.getByRole("heading", { name: fixture.heading })).toBeVisible({ timeout: 240_000 });
  console.info(`Browser acceptance: ${fixture.framework} iframe loaded`);
  if (fixture.framework !== "http") {
    if (fixture.framework === "next") await expect(frame.locator("body")).toHaveAttribute("data-hydrated", "yes", { timeout: 30_000 });
    await frame.getByRole("button", { name: "Count 0", exact: true }).click();
    await expect(frame.getByRole("button", { name: "Count 1", exact: true })).toBeVisible();
    await expect.poll(() => developmentFrames, { timeout: 30_000 }).toBeGreaterThan(0);
  }
  if (fixture.framework === "vite") {
    const revision = page.getByText(/^Revision /); const previous = await revision.textContent();
    await reviseSource(page, chat.path, files["main.js"], fixture.files["main.js"].replace("Vite browser preview", "Vite browser revision 2").replace("useState(0)", "useState(10)"));
    await page.getByRole("button", { name: "Refresh from changes", exact: true }).click();
    await expect(revision).not.toHaveText(previous!);
    await expect(frame.getByRole("heading", { name: "Vite browser revision 2" })).toBeVisible({ timeout: 180_000 });
    await frame.getByRole("button", { name: "Count 10", exact: true }).click();
    await expect(frame.getByRole("button", { name: "Count 11", exact: true })).toBeVisible();
    console.info("Browser acceptance: revised Vite source loaded in a new preview revision");
  }
  if (fixture.framework === "next") {
    await frame.getByRole("button", { name: "Send POST", exact: true }).click();
    await expect(frame.locator("output")).toContainText('"hello":"next-browser"');
  }
  if (fixture.framework === "http") {
    await frame.getByRole("button", { name: "Send POST", exact: true }).click();
    await expect(frame.locator("pre")).toContainText('"hello":"browser"');
    await expect(frame.locator("pre")).toContainText('"cookie":null');
    await expect(frame.locator("pre")).toContainText('"authorization":null');
    await frame.getByRole("button", { name: "Send WebSocket", exact: true }).click();
    await expect(frame.locator("pre")).toHaveText("echo:browser-ping");
  }
  await expect(page.locator('iframe[title="App preview"]')).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-downloads");
  await info.attach("framework-acceptance-chat.json", { body: JSON.stringify({ conversation_id: chat.id, framework: fixture.framework }), contentType: "application/json" });
  await page.screenshot({ path: info.outputPath(`${fixture.framework}-browser.png`), fullPage: true });
});

test("real files, revision edits, PTY reconnect, and opaque interactive preview", async ({ page }, info) => {
  const errors: string[] = []; page.on("pageerror", (error) => errors.push(error.message));
  const chat = await start(page);
  await upload(page, "sales.csv", "text/csv", "region,total\nNorth,42\nSouth,18\n");
  await upload(page, "chart.html", "text/html", '<!doctype html><h1>Acceptance chart v1</h1><button id="increment">Increment</button><output id="count">0</output><button id="scope">Check isolation</button><script>let n=0;increment.onclick=()=>count.textContent=++n;scope.onclick=()=>{try{parent.document.body.innerHTML="bad"}catch{scope.textContent="Isolated"}}</script>');
  console.info("Browser acceptance: CSV and HTML uploads ready");
  await page.getByRole("button", { name: /Files & workspace/ }).click();
  await page.getByRole("button", { name: /^sales\.csv/ }).click();
  await page.getByRole("button", { name: "Take control", exact: true }).click();
  await expect(page.getByRole("button", { name: "Return to agent", exact: true })).toBeVisible();
  await page.locator(".cm-content").fill("region,total\nNorth,50\nSouth,18\n");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Version" })).toContainText("v2", { timeout: 60_000 });
  console.info("Browser acceptance: editor saved CSV version 2");
  await page.getByRole("button", { name: "All files", exact: true }).click();
  await page.getByText("Working directory", { exact: true }).click();
  await expect(page.getByText("/workspace", { exact: true }).first()).toBeVisible();
  await expect(page.locator("details").filter({ has: page.getByText("Working directory", { exact: true }) }).getByRole("button", { name: /^sales\.csv/ })).toBeVisible();
  await page.getByRole("tab", { name: "terminal", exact: true }).click();
  await page.getByRole("button", { name: "Open terminal", exact: true }).click();
  const terminalStatus = () => page.locator('[aria-label="Interactive terminal"]').locator("..").getByRole("status");
  await expect(terminalStatus()).toHaveText(/^(Connected|running)$/, { timeout: 60_000 });
  const marker = `jhin-terminal-${randomUUID()}`;
  await page.locator(".xterm-helper-textarea").focus(); await page.keyboard.type(`printf '${marker}%s\\n' "$((6 * 7))"`); await page.keyboard.press("Enter");
  await expect.poll(async () => (await (await page.request.get(`${chat.path}/runtime`)).json()).terminal.output, { timeout: 30_000 }).toContain(`${marker}42`);
  console.info("Browser acceptance: real PTY command output verified");
  await page.reload(); await page.getByRole("button", { name: /Files & workspace/ }).click(); await page.getByRole("tab", { name: "terminal", exact: true }).click();
  await expect(terminalStatus()).toHaveText(/^(Connected|running)$/);
  console.info("Browser acceptance: PTY reconnect verified");
  await page.getByRole("button", { name: "Interrupt", exact: true }).click();
  await page.getByRole("button", { name: "Close terminal", exact: true }).click();
  await expect(page.getByRole("button", { name: "New terminal", exact: true })).toBeVisible();
  const files = (await (await page.request.get(`${chat.path}/files`)).json()).items;
  const html = files.find((file: { name: string }) => file.name === "chart.html");
  await page.getByRole("tab", { name: "preview", exact: true }).click();
  await page.getByRole("combobox", { name: "Preview source" }).selectOption(html.id);
  await page.getByRole("button", { name: "Open preview", exact: true }).click();
  const frame = page.frameLocator('iframe[title="App preview"]');
  await frame.getByRole("button", { name: "Increment" }).click(); await expect(frame.locator("output")).toHaveText("1");
  await frame.getByRole("button", { name: "Check isolation" }).click(); await expect(frame.getByRole("button", { name: "Isolated" })).toBeVisible();
  await expect(page.locator('iframe[title="App preview"]')).toHaveAttribute("sandbox", "allow-scripts allow-forms allow-downloads");
  const revision = page.getByText(/^Revision /); const previous = await revision.textContent();
  await reviseSource(page, chat.path, html, '<!doctype html><h1>Acceptance chart v2</h1><button id="increment">Increment revised</button><output id="count">10</output><script>let n=10;increment.onclick=()=>count.textContent=++n</script>');
  await page.getByRole("button", { name: "Refresh from changes", exact: true }).click();
  await expect(revision).not.toHaveText(previous!);
  await expect(frame.getByRole("heading", { name: "Acceptance chart v2" })).toBeVisible({ timeout: 60_000 });
  await frame.getByRole("button", { name: "Increment revised" }).click(); await expect(frame.locator("output")).toHaveText("11");
  console.info("Browser acceptance: revised HTML artifact loaded and interactive");
  await page.screenshot({ path: info.outputPath("real-preview.png"), fullPage: true });
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await page.getByRole("button", { name: "Return to agent", exact: true }).click();
  await page.reload(); await page.getByRole("button", { name: /Files & workspace/ }).click();
  await page.getByRole("button", { name: /^sales\.csv/ }).click();
  await expect(page.getByRole("combobox", { name: "Version" })).toContainText("v2");
  await expect(page.locator(".cm-content")).toContainText("North,50");
  await info.attach("acceptance-chat.json", { body: JSON.stringify({ conversation_id: chat.id, files: files.map((file: { id: string; name: string; current_revision_id: string }) => ({ id: file.id, name: file.name, revision_id: file.current_revision_id })) }, null, 2), contentType: "application/json" });
  expect(errors).toEqual([]);
});

test("real agent sees an uploaded spreadsheet and publishes a managed deliverable", async ({ page }, info) => {
  test.skip(process.env.JHIN_LIVE_AGENT_GENERATION !== "1", "Model execution must be explicitly enabled.");
  test.setTimeout(300_000);
  const chat = await start(page);
  await upload(page, "sales.csv", "text/csv", "region,total\nNorth,42\nSouth,18\n");
  await page.getByRole("textbox", { name: "Message", exact: true }).fill("Analyze the attached CSV. Use your real terminal to calculate its total, create a short editable report.txt in this chat workspace, and publish it using the managed artifact tool. Reply with the total and the published file. Do not access external apps.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.getByRole("article", { name: /File:.*report/ })).toBeVisible({ timeout: 240_000 });
  const files = (await (await page.request.get(`${chat.path}/files`)).json()).items;
  expect(files.some((file: { kind: string; name: string }) => file.kind === "artifact" && file.name.includes("report"))).toBe(true);
  await info.attach("agent-acceptance-chat.json", { body: JSON.stringify({ conversation_id: chat.id }), contentType: "application/json" });
  await page.screenshot({ path: info.outputPath("real-agent-deliverable.png"), fullPage: true });
});
