import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { consumeEventFrames, mergeConversationItems, messageInputDraft, type ConversationItem } from "@/lib/agentic-chat";
import { useConversationJournal } from "@/lib/use-conversation-journal";
const item = (revision: number, text: string, id = "generation:a"): ConversationItem => ({ id, version: 1, sequence: revision, revision, kind: "generation", status: "streaming", actor: { type: "agent", id: "agent" }, task_id: "task", run_id: "run", created_at: "2026-09-11T00:00:00Z", data: { text } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
describe("durable conversation updates", () => {
  it("preserves the original attachment version when editing a message", () => {
    expect(messageInputDraft({ text: "Revise this", attachments: [{ type: "file", id: "f", revision_id: "old-version", name: "report.docx" }], context_refs: [{ type: "agent", id: "bisby", name: "Bisby" }] })).toEqual({ text: "Revise this", attachment_ids: ["f"], context_refs: [{ kind: "file", id: "f", version_id: "old-version", label: "report.docx" }, { kind: "agent", id: "bisby", label: "Bisby" }] });
  });
  it("does not request snapshot or events when the rollout is disabled", async () => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    function Probe() { useConversationJournal("w", "c", false); return <p>Legacy chat</p>; }
    const client = new QueryClient(); render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
    await act(async () => {}); expect(fetchMock).not.toHaveBeenCalled(); client.clear();
  });
  it("upserts retransmitted snapshots and rejects older revisions without concatenating attempts", () => {
    const result = mergeConversationItems([item(2, "Hello")], [item(1, "He"), item(2, "Hello"), item(3, "Hello world"), item(1, "A different attempt", "generation:b")]);
    expect(result).toHaveLength(2); expect(result[0].data.text).toBe("Hello world"); expect(result[1].data.text).toBe("A different attempt");
  });
  it("keeps incomplete frames and joins multiline data while ignoring heartbeat comments", () => {
    const first = consumeEventFrames(': heartbeat\r\n\r\nid: 12\r\nevent: item\r\ndata: {"id":\r\ndata: "x"}\r\n\r\nevent: item\ndata: par');
    expect(first.events).toEqual([{ event: "item", id: "12", data: '{"id":\n"x"}' }]);
    expect(consumeEventFrames(first.rest + "tial\n\n").events[0].data).toBe("partial");
  });
  it("renders a snapshot, then replaces live output using the committed replay cursor", async () => {
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => String(url).includes("/items") ? new Response(JSON.stringify({ items: [item(1, "First")], cursor: 10, next_before: null, has_more: false, version: 1 })) : new Response(new ReadableStream({ start(controller) { stream = controller; } }), { headers: { "content-type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetchMock);
    function Probe() { const journal = useConversationJournal("w", "c"); return <div><span>{journal.status}</span>{journal.items.map((entry) => <p key={entry.id}>{String(entry.data.text)}</p>)}</div>; }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
    await screen.findByText("connected"); expect(screen.getByText("First")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/events?after=10"))).toBe(true);
    await act(async () => { stream.enqueue(new TextEncoder().encode(`id: 11\nevent: item\ndata: ${JSON.stringify(item(11, "Final draft"))}\n\n`)); });
    await screen.findByText("Final draft"); expect(screen.queryByText("First")).toBeNull();
    await act(async () => { stream.enqueue(new TextEncoder().encode(`id: 11\nevent: item\ndata: ${JSON.stringify(item(11, "Final draft"))}\n\n`)); });
    expect(screen.getAllByText("Final draft")).toHaveLength(1);
    cleanup(); stream.close(); client.clear();
  });
  it("falls back to legacy polling when the journal is disabled without clearing content", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Feature disabled" }), { status: 404 })));
    function Probe() { const journal = useConversationJournal("w", "c"); return <span>{journal.status}</span>; }
    const client = new QueryClient(); render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByText("polling")).toBeTruthy()); client.clear();
  });
  it("replays updates to older loaded actions without acknowledging a newer snapshot cursor", async () => {
    vi.useFakeTimers();
    const streams: ReadableStreamDefaultController<Uint8Array>[] = [];
    const recent = Array.from({ length: 50 }, (_, index) => item(index + 51, `Recent ${index}`, `message:${index}`));
    const fetchMock = vi.fn(async (raw: RequestInfo | URL) => {
      const url = new URL(String(raw), "http://localhost");
      if (url.pathname.endsWith("/items")) return new Response(JSON.stringify(url.searchParams.has("before")
        ? { items: [item(1, "Old action running", "action:old")], cursor: 100, next_before: null }
        : { items: recent, cursor: 100, next_before: 51 }));
      return new Response(new ReadableStream({ start(controller) { streams.push(controller); } }), { headers: { "content-type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    function Probe() { const journal = useConversationJournal("w", "c"); return <><span>{journal.status}</span><button onClick={journal.loadEarlier}>Earlier</button>{journal.items.map((entry) => <p key={entry.id}>{String(entry.data.text)}</p>)}</>; }
    const client = new QueryClient(); render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByText("Earlier")); });
    expect(screen.getByText("Old action running")).toBeTruthy();
    await act(async () => { streams[0].close(); });
    expect(screen.getByText("Old action running")).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    const requests = fetchMock.mock.calls.map(([url]) => String(url));
    expect(requests.filter((url) => url.includes("/items"))).toHaveLength(2);
    expect(requests.filter((url) => url.endsWith("/events?after=100"))).toHaveLength(2);
    await act(async () => { streams[1].enqueue(new TextEncoder().encode(`id: 151\nevent: item\ndata: ${JSON.stringify(item(151, "Old action completed", "action:old"))}\n\n`)); });
    expect(screen.getByText("Old action completed")).toBeTruthy(); expect(screen.queryByText("Old action running")).toBeNull();
    client.clear();
  });
  it("recovers loaded identities outside the latest fifty and reconnects from the first snapshot cursor", async () => {
    vi.useFakeTimers();
    const streams: ReadableStreamDefaultController<Uint8Array>[] = []; let snapshots = 0;
    const latest = Array.from({ length: 50 }, (_, index) => item(index + 151, `New ${index}`, `message:new-${index}`));
    const fetchMock = vi.fn(async (raw: RequestInfo | URL) => {
      const url = new URL(String(raw), "http://localhost");
      if (url.pathname.endsWith("/items")) {
        if (url.searchParams.has("item_id")) return new Response(JSON.stringify({ items: [item(201, "Recovered old action", "action:old")], cursor: 201, next_before: null }));
        if (url.searchParams.has("before")) return new Response(JSON.stringify({ items: [item(125, "Recovered middle", "message:middle")], cursor: 201, next_before: 101 }));
        snapshots += 1;
        return new Response(JSON.stringify(snapshots === 1 ? { items: [item(1, "Old action", "action:old")], cursor: 100, next_before: 1 } : { items: latest, cursor: 200, next_before: 151 }));
      }
      return new Response(new ReadableStream({ start(controller) { streams.push(controller); } }), { headers: { "content-type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    function Probe() { const journal = useConversationJournal("w", "c"); return <><span>{journal.status}</span><button onClick={journal.loadEarlier}>Earlier</button>{journal.items.map((entry) => <p key={entry.id}>{String(entry.data.text)}</p>)}</>; }
    const client = new QueryClient(); render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
    await act(async () => {});
    await act(async () => { streams[0].enqueue(new TextEncoder().encode("event: snapshot_required\ndata: {}\n\n")); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("Recovered old action")).toBeTruthy(); expect(screen.getByText("New 49")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("item_id=action%3Aold"))).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/events?after=200"))).toBe(true);
    expect(fetchMock.mock.calls).toHaveLength(5);
    await act(async () => { fireEvent.click(screen.getByText("Earlier")); });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/items?before=151"))).toBe(true);
    expect(screen.getByText("Recovered middle")).toBeTruthy(); client.clear();
  });
  it("resets identities and cursors when switching conversations and ignores an old in-flight page", async () => {
    let releaseEarlier!: (response: Response) => void;
    const fetchMock = vi.fn(async (raw: RequestInfo | URL) => {
      const url = String(raw), second = url.includes("/second/");
      if (url.includes("before=")) return new Promise<Response>((resolve) => { releaseEarlier = resolve; });
      if (url.includes("/items")) return new Response(JSON.stringify({ items: [item(1, second ? "Second chat" : "First chat")], cursor: second ? 5 : 100, next_before: second ? null : 1 }));
      return new Response(new ReadableStream(), { headers: { "content-type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    function Probe({ id }: { id: string }) { const journal = useConversationJournal("w", id); return <><button onClick={journal.loadEarlier}>Earlier</button>{journal.items.map((entry) => <p key={entry.id}>{String(entry.data.text)}</p>)}</>; }
    const client = new QueryClient(); const view = render(<QueryClientProvider client={client}><Probe id="first" /></QueryClientProvider>);
    await screen.findByText("First chat"); fireEvent.click(screen.getByText("Earlier"));
    view.rerender(<QueryClientProvider client={client}><Probe id="second" /></QueryClientProvider>);
    await screen.findByText("Second chat");
    await act(async () => { releaseEarlier(new Response(JSON.stringify({ items: [item(1, "Old page", "message:old")], cursor: 100, next_before: null }))); });
    expect(screen.queryByText("First chat")).toBeNull(); expect(screen.queryByText("Old page")).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/second/events?after=5"))).toBe(true); client.clear();
  });
});
