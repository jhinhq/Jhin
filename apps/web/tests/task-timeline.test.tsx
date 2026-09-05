/** The run timeline names what a step was offered, not just what it called. */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Timeline } from "@/app/(app)/tasks/[id]/view";
import type { RunEvent } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/tasks/task-1",
  useSearchParams: () => new URLSearchParams(),
}));

afterEach(cleanup);

function event(event_type: string, payload_json: Record<string, unknown>, seq = 1): RunEvent {
  return {
    id: `event-${seq}`,
    run_id: "run-1",
    seq,
    event_type,
    payload_json,
    created_at: "2026-09-05T12:03:51Z",
  };
}

describe("Timeline", () => {
  it("renders the tools a step offered with the count, step and names", () => {
    render(
      <Timeline
        live={false}
        events={[
          event("agent.step.tools_offered", {
            step: 0,
            count: 3,
            tools: ["github.repository.read", "github.branch.list", "memory.recall"],
            truncated: false,
          }),
        ]}
      />,
    );

    expect(screen.getByText("Tools offered")).toBeDefined();
    const offered = screen.getByTestId("tools-offered");
    expect(offered.textContent).toContain("3 tools · step 1");
    expect(offered.textContent).not.toContain("list truncated");
    fireEvent.click(within(offered).getByText("Show tools"));
    expect(within(offered).getByText("github.repository.read").tagName).toBe("CODE");
    expect(within(offered).getByText("memory.recall")).toBeDefined();
  });

  it("says when the list was truncated and when nothing was offered", () => {
    render(
      <Timeline
        live={false}
        events={[
          event("agent.step.tools_offered", { step: 2, count: 300, tools: ["a"], truncated: true }, 1),
          event("agent.step.tools_offered", { step: 3, count: 0, tools: [], truncated: false }, 2),
        ]}
      />,
    );

    expect(screen.getByTestId("tools-offered").textContent).toContain("300 tools · step 3 · list truncated");
    const empty = screen.getByTestId("tools-offered-empty");
    expect(empty.textContent).toContain("No tools offered");
    expect(empty.textContent).toContain("This agent had no grant that advertises on this kind of task.");
  });

  it("still falls back to the raw type for events it does not know", () => {
    render(<Timeline live={false} events={[event("node.observe", { detail: "looked" })]} />);
    expect(screen.getByText("node.observe")).toBeDefined();
    expect(screen.getByText(/looked/)).toBeDefined();
  });
});
