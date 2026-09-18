import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemorySummary } from "@/components/agents/memory-summary";
import { api } from "@/lib/api";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("shows bounded supported summary, current source version and provenance", async () => {
  vi.mocked(api).mockResolvedValue({ scope: "team", scope_id: "t1", version: "digest-123", summary: "Write a daily draft. The director publishes only reviewed revisions.", coverage_count: 2, source_count: 1, generated_at: "2026-09-12T00:00:00Z", stale: false, items: [{id:"m1",version:3,content:"The director reviews each draft.",source_conversation_id:"c1",source_message_id:"msg1",source_task_id:"task1"}] });
  render(<QueryClientProvider client={new QueryClient()}><MemorySummary workspaceId="ws" scope="team" scopeId="t1" canRebuild /></QueryClientProvider>);
  fireEvent.click(screen.getByText("Current memory summary"));
  expect(await screen.findByText(/Write a daily draft/)).toBeDefined();
  expect(screen.getByText(/2 supported memories/)).toBeDefined();
  fireEvent.click(screen.getByText("Sources and versions"));
  expect(screen.getByRole("link", { name: "Source chat" }).getAttribute("href")).toBe("/chats/c1");
  expect(screen.getByText(/Memory version 3/)).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Refresh summary" }));
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/memories/summary/rebuild", expect.objectContaining({ method:"POST", params:{scope:"team",scope_id:"t1"} }));
});
