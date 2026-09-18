import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Markdown } from "@/components/markdown";
import { Composer } from "@/components/chat/composer";
import { ActionCard } from "@/components/chat/action-card";
import { AttachmentTray } from "@/components/chat/attachments";
import type { ManagedFile } from "@/lib/workspace-files";
import type { ConversationToolCall } from "@/lib/types";
afterEach(cleanup);
describe("agentic content", () => {
  it("previews the pinned image revision and labels an earlier version accurately", () => {
    const file = { id: "image", name: "chart.png", preview_kind: "image", preview_url: "/api/files/image/preview", current_revision_id: "new-revision", version: 2, size_bytes: 100 } as ManagedFile;
    const { container } = render(<AttachmentTray files={[file]} uploads={[]} onRemove={vi.fn()} onCancel={vi.fn()} revisionIds={{ image: "old-revision" }} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/api/files/image/preview?revision_id=old-revision");
    expect(screen.getByText(/Selected earlier version/)).toBeTruthy(); expect(screen.queryByText(/v2/)).toBeNull();
  });
  it("renders real GFM tables, nested lists and task checkboxes", () => {
    const { container } = render(<Markdown variant="chat" source={"| Name | Count |\n| --- | --- |\n| Report | 2 |\n\n- [x] Done\n- Parent\n  - Child"} />);
    expect(screen.getByRole("table")).toBeTruthy(); expect(screen.getByRole("checkbox")).toHaveProperty("checked", true); expect(container.querySelectorAll("ul ul")).toHaveLength(1);
  });
  it("never makes hostile links or HTML executable, including image sources", () => {
    const { container } = render(<Markdown variant="chat" source={'[run](javascript:alert%281%29) ![x](data:image/svg+xml,evil)\n\n<script>alert(1)</script>'} />);
    expect(container.querySelector("script,a,img")).toBeNull(); expect(container.textContent).toContain("javascript:");
  });
  it("renders math without permitting trusted HTML extensions", () => {
    const { container } = render(<Markdown variant="chat" source={"Energy is $E=mc^2$."} />);
    expect(container.querySelector("math")).toBeTruthy(); expect(container.textContent).toContain("Energy is");
  });
  it("keeps typing available during uploads and accepts attachment-only messages once ready", () => {
    const send = vi.fn(); const { rerender } = render(<Composer value="" onChange={() => {}} onSend={send} hasAttachments uploading />);
    expect(screen.getByRole("textbox")).toHaveProperty("disabled", false); expect(screen.getByRole("button", { name: "Processing attachments…" })).toHaveProperty("disabled", true);
    rerender(<Composer value="" onChange={() => {}} onSend={send} hasAttachments />); fireEvent.click(screen.getByRole("button", { name: "Send message" })); expect(send).toHaveBeenCalledWith("");
  });
  it("shows an app failure and its sanitized output in compact activity", () => {
    const call: ConversationToolCall = { id: "call", run_id: "run", agent_id: "agent", task_id: "task", agent_name: "Bisby", tool_name: "supabase.list_projects", sanitized_input_json: {}, sanitized_output_json: { error: "Connection expired" }, status: "failed", approval_id: null, started_at: null, completed_at: null, duration_ms: null, error_code: null, created_at: "2026-09-11T00:00:00Z" };
    render(<ActionCard call={call} detail="compact" />); expect(screen.getByText("Failed")).toBeTruthy(); expect(screen.getAllByText(/Connection expired/).length).toBeGreaterThan(0);
  });
});
