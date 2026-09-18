import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SchedulesPanel } from "@/components/automations/schedules-panel";
import { api, ApiError } from "@/lib/api";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const schedule = { id: "s1", workspace_id: "ws", name: "Daily blog", agent_id: "a1", brief: "Prepare a draft for review. Never publish without approval.", local_time: "09:00", timezone: "America/Los_Angeles", weekdays: [0,1,2,3,4], enabled: true, version: 2, next_run_at: "2026-09-14T16:00:00Z", last_run_at: null, last_status: null, overlap_policy: "skip", created_at: "2026-09-12T00:00:00Z", updated_at: "2026-09-12T00:00:00Z", deleted_at: null };
function setup(canWrite = true) { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SchedulesPanel workspaceId="ws" agents={[{ id: "a1", name: "Blogger" }]} canWrite={canWrite} /></QueryClientProvider>); }
it("shows the selected timezone and standing brief, pauses using version checks", async () => {
  vi.mocked(api).mockResolvedValue({ items: [schedule], total: 1 }); setup();
  await screen.findByText("Daily blog");
  expect(screen.getByText(schedule.brief)).toBeDefined(); expect(screen.getByText(/America\/Los_Angeles/)).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Pause schedule" }));
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/schedules/s1", expect.objectContaining({ method: "PATCH", body: { expected_version: 2, enabled: false } }));
});
it("shows an actionable stale edit without claiming it was saved", async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => { if(options?.method) throw new ApiError(409, "Stale"); return { items: [schedule], total: 1 }; }); setup();
  await screen.findByText("Daily blog"); fireEvent.click(screen.getByRole("button", { name: "Edit schedule" }));
  fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringMatching(/changed.*reload/i));
  expect(screen.getByRole("dialog")).toBeDefined();
});
it("exposes occurrence history and linked tasks to viewers", async () => {
  vi.mocked(api).mockImplementation(async (path) => path.endsWith("/occurrences") ? { items: [{ id: "o1", scheduled_for: schedule.next_run_at, status: "completed", task_id: "t1" }], total: 1 } : { items: [schedule], total: 1 }); setup(false);
  await screen.findByText("Daily blog"); fireEvent.click(screen.getByRole("button", { name: "Run history" }));
  expect((await screen.findByRole("link", { name: "View task" })).getAttribute("href")).toBe("/tasks/t1");
  expect(screen.queryByRole("button", { name: "Pause schedule" })).toBeNull();
});
