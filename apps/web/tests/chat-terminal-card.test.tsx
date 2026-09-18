import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TerminalCard } from "@/components/chat/terminal-card";
import { groupExchanges, mergeTimeline } from "@/lib/chat";
import type { ConversationToolCall } from "@/lib/types";

afterEach(cleanup);

function call(overrides: Partial<ConversationToolCall> = {}): ConversationToolCall {
  return {
    id: "call-1", task_id: "task-1", run_id: "run-1", agent_id: "bisby", agent_name: "Bisby",
    tool_name: "cli.command.execute", sanitized_input_json: { input: { command: "printf first; sleep 5; printf second" } },
    sanitized_output_json: {}, status: "executing", approval_id: null,
    started_at: "2026-09-11T12:00:00Z", completed_at: null, duration_ms: null, error_code: null,
    created_at: "2026-09-11T12:00:00Z", sandbox_job: null, ...overrides,
  };
}

describe("terminal execution transcript", () => {
  it("shows the actual command before output, then replaces bounded snapshots and records exit status", () => {
    const original = call();
    const { rerender } = render(<TerminalCard call={original} />);
    expect(screen.getByText("printf first; sleep 5; printf second")).toBeTruthy();
    expect(screen.getByText("Starting")).toBeTruthy();
    const running = { ...original, sandbox_job: {
      job_id: "job-1", status: "running", network_policy: "none", stdout: "first\n", stderr: "",
      exit_code: null, started_at: original.started_at, completed_at: null, duration_ms: null,
    } };
    rerender(<TerminalCard call={running} />);
    expect(screen.getByText("Running")).toBeTruthy();
    expect(screen.getByLabelText("Standard output").textContent).toBe("first\n");
    rerender(<TerminalCard call={{ ...running, status: "completed", sandbox_job: {
      ...running.sandbox_job, status: "completed", stdout: "first\nsecond\n", exit_code: 0,
      completed_at: "2026-09-11T12:00:05Z", duration_ms: 5000,
    } }} />);
    expect(screen.getByLabelText("Standard output").textContent).toBe("first\nsecond\n");
    expect(screen.getByText("Completed")).toBeTruthy();
    expect(screen.getByText("Exit 0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Hide output/ }));
    expect(screen.queryByLabelText("Standard output")).toBeNull();
  });

  it("renders stdout/stderr as plain text and preserves failed historical output without a sandbox row", () => {
    render(<TerminalCard call={call({ status: "completed", completed_at: "2026-09-11T12:00:05Z",
      sanitized_output_json: { status: "failed", exit_code: 2, stdout: "<script>bad()</script>", stderr: "missing file" },
    })} />);
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("Exit 2")).toBeTruthy();
    expect(screen.getByLabelText("Standard output").textContent).toBe("<script>bad()</script>");
    expect(screen.getByLabelText("Standard error").textContent).toBe("missing file");
    expect(document.querySelector("script")).toBeNull();
  });

  it("keeps real CLI calls visible in quiet mode, deduplicated, outside colleague exchanges", () => {
    const terminal = call();
    const items = mergeTimeline([], [], { detailed: false, toolCalls: [terminal, terminal] });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "tool", id: "tool:call-1" });
    expect(groupExchanges(items)).toEqual(items);
  });

  it("labels file operations without pretending a generated shell command was requested", () => {
    render(<TerminalCard call={call({ tool_name: "cli.file.read", sanitized_input_json: { path: "requirements.txt" } })} />);
    expect(screen.getByText("Read file")).toBeTruthy();
    expect(screen.getByText("requirements.txt")).toBeTruthy();
    expect(screen.queryByText("Completed")).toBeNull();
  });

  it.each([
    ["completed", "Completed"], ["failed", "Failed"], ["denied", "Not allowed"],
    ["pending_approval", "Needs approval"], ["pending_review", "Needs review"],
  ])("shows persisted %s status and the recorded reason", (status, label) => {
    render(<TerminalCard call={call({ status, sanitized_output_json: { reason: "This command needs internet access." } })} />);
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.getByText("This command needs internet access.")).toBeTruthy();
  });

  it("shows a silent running command and does not mislabel the test executor's arbitrary command", () => {
    render(<TerminalCard call={call({ tool_name: "cli.test.run", sandbox_job: {
      job_id: "job-1", status: "running", network_policy: "none", stdout: "", stderr: "",
      exit_code: null, started_at: null, completed_at: null, duration_ms: null,
    } })} />);
    expect(screen.getByText("Running")).toBeTruthy();
    expect(screen.getByText("Terminal")).toBeTruthy();
  });
});
