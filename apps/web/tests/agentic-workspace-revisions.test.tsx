import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { WorkspaceFileView } from "@/components/chat/workspace-file-view";
import type { ManagedFile } from "@/lib/workspace-files";
import type { ConversationRuntime } from "@/lib/workspace-runtime";
vi.mock("@/components/chat/workspace-editor", () => ({ default: ({ value, onChange, readOnly }: { value: string; onChange: (value: string) => void; readOnly: boolean }) => <textarea aria-label="File editor" value={value} onChange={(event) => onChange(event.target.value)} readOnly={readOnly} /> }));
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });
const file: ManagedFile = { id: "f", conversation_id: "c", workspace_id: "w", name: "report.txt", path: "report.txt", kind: "artifact", status: "ready", error: null, mime_type: "text/plain", size_bytes: 7, preview_kind: "text", current_revision_id: "r1", version: 1, sha256: "first", extracted_text: "initial", extraction_truncated: false, created_at: "2026-09-11", updated_at: "2026-09-11", download_url: "/download", preview_url: "/preview" };
const runtime = { lease_generation: 7 } as ConversationRuntime;
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

it("reopens an unsaved editor after handoff against its original base when the agent publishes a new version", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => String(url).includes("/versions") ? response({ items: [{ id: "r1", version: 1 }, { id: "r2", version: 2 }] }) : response({ content: "initial", editable: true })));
  const first = render(<QueryClientProvider client={client}><WorkspaceFileView file={file} workspaceId="w" runtime={runtime} canEdit onUse={vi.fn()} /></QueryClientProvider>);
  fireEvent.change(await screen.findByRole("textbox", { name: "File editor" }), { target: { value: "unsaved human work" } });
  first.unmount();
  render(<QueryClientProvider client={client}><WorkspaceFileView file={{ ...file, current_revision_id: "r2", version: 2 }} workspaceId="w" runtime={runtime} canEdit={false} onUse={vi.fn()} /></QueryClientProvider>);
  expect(await screen.findByRole("textbox", { name: "File editor" })).toHaveProperty("value", "unsaved human work");
  expect(screen.getByRole("textbox", { name: "File editor" })).toHaveProperty("readOnly", true);
  expect(screen.getByRole("combobox")).toHaveProperty("value", "r1");
  expect(screen.getByText(/saved file changed while you were editing/)).toBeTruthy(); client.clear();
});

it("retains edits across a conflict, compares a fresh snapshot, and saves against the selected base", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const saves: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const value = String(url);
    if (init?.method === "PUT") { saves.push(JSON.parse(String(init.body))); return saves.length === 1 ? response({ detail: "revision_conflict" }, 409) : response({ ...file, current_revision_id: "r3", version: 3 }); }
    if (value.endsWith("/files/publish")) return response({ ...file, current_revision_id: "r2", version: 2 });
    if (value.includes("/versions")) return response({ items: [{ id: "r1", version: 1 }, { id: "r2", version: 2 }, { id: "r3", version: 3 }] });
    return response({ file_id: "f", revision_id: value.includes("r2") ? "r2" : "r1", content: value.includes("r2") ? "changed elsewhere" : "initial", editable: true, truncated: false });
  }));
  const props = { file, workspaceId: "w", runtime, canEdit: true, onUse: vi.fn() };
  const { rerender } = render(<QueryClientProvider client={client}><WorkspaceFileView {...props} /></QueryClientProvider>);
  fireEvent.change(await screen.findByRole("textbox", { name: "File editor" }), { target: { value: "my retained edit" } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  fireEvent.click(await screen.findByRole("button", { name: "Compare with current workspace file" }));
  await screen.findByText("changed elsewhere");
  expect(screen.getByRole("textbox", { name: "File editor" })).toHaveProperty("value", "my retained edit");
  rerender(<QueryClientProvider client={client}><WorkspaceFileView {...props} file={{ ...file, current_revision_id: "r2", version: 2 }} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Use this version as save base" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).not.toHaveProperty("disabled", true));
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(saves).toHaveLength(2));
  expect(saves[0]).toEqual({ content: "my retained edit", expected_revision_id: "r1", lease_generation: 7 });
  expect(saves[1]).toEqual({ content: "my retained edit", expected_revision_id: "r2", lease_generation: 7 });
  client.clear();
});

it("pins annotations and reuse to the viewed version instead of the latest file", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const use = vi.fn(), feedback = vi.fn(), requests: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { requests.push(JSON.parse(String(init.body))); return response({ id: "annotation" }); }
    return String(url).includes("/versions") ? response({ items: [{ id: "r1", version: 1 }, { id: "r2", version: 2 }] }) : response({ content: "Old document", editable: false });
  }));
  render(<QueryClientProvider client={client}><WorkspaceFileView file={{ ...file, current_revision_id: "r2", version: 2, preview_kind: "document" }} workspaceId="w" canEdit={false} onUse={use} onFeedback={feedback} /></QueryClientProvider>);
  await screen.findByRole("option", { name: "v1" }); fireEvent.change(screen.getByRole("combobox"), { target: { value: "r1" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Feedback on this version" }), { target: { value: "Shorten section 2" } });
  fireEvent.click(screen.getByRole("button", { name: "Add feedback to message" }));
  await waitFor(() => expect(use).toHaveBeenCalledWith(expect.objectContaining({ id: "f" }), "r1"));
  expect(requests[0]).toEqual({ revision_id: "r1", text: "Shorten section 2", location: {} });
  expect(feedback).toHaveBeenCalledWith(expect.stringContaining("Shorten section 2")); client.clear();
});
