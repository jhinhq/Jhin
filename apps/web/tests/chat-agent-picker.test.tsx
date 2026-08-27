/** Component tests: choosing who to talk to, and the live pill that says
 * what they are doing. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentPicker } from "@/components/chat/agent-picker";
import { LiveStatusPill } from "@/components/chat/status-pill";
import type { Conversation } from "@/lib/types";

afterEach(cleanup);

type PickerAgent = React.ComponentProps<typeof AgentPicker>["agents"][number];

function agent(overrides: Partial<PickerAgent> & { id: string; name: string }): PickerAgent {
  return {
    role_title: "Analyst",
    status: "active",
    avatar_url: null,
    avatar_shape: null,
    avatar_color: null,
    ...overrides,
  };
}

const AGENTS = [
  agent({ id: "a1", name: "Scout" }),
  agent({ id: "a2", name: "Linus", role_title: "Engineer" }),
];

describe("AgentPicker", () => {
  it("is a single radio group so arrow keys and screen readers treat it as one choice", () => {
    render(<AgentPicker agents={AGENTS} selectedId="a1" onSelect={() => {}} />);
    const group = screen.getByRole("radiogroup", { name: "Choose an agent" });
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    for (const radio of screen.getAllByRole("radio")) expect(group.contains(radio)).toBe(true);
  });

  it("marks exactly one option as chosen", () => {
    render(<AgentPicker agents={AGENTS} selectedId="a2" onSelect={() => {}} />);
    expect(screen.getByRole("radio", { name: /Scout/ }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("radio", { name: /Linus/ }).getAttribute("aria-checked")).toBe("true");
  });

  it("marks nothing as chosen before a selection exists", () => {
    // The chats home renders with `selectedId={null}` while agents load.
    render(<AgentPicker agents={AGENTS} selectedId={null} onSelect={() => {}} />);
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio.getAttribute("aria-checked")).toBe("false");
    }
  });

  it("reports the id of the agent that was clicked", () => {
    const onSelect = vi.fn();
    render(<AgentPicker agents={AGENTS} selectedId="a1" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("radio", { name: /Linus/ }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("a2");
  });

  it("still reports a click on the current selection", () => {
    // The caller also uses the callback to remember the agent, so a click on
    // the already-selected chip is not a no-op it can drop.
    const onSelect = vi.fn();
    render(<AgentPicker agents={AGENTS} selectedId="a1" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("radio", { name: /Scout/ }));
    expect(onSelect).toHaveBeenCalledWith("a1");
  });

  it("shows the agent's name and role, and drops an empty role line", () => {
    render(
      <AgentPicker
        agents={[AGENTS[0], agent({ id: "a3", name: "Nova", role_title: "" })]}
        selectedId={null}
        onSelect={() => {}}
      />,
    );
    const scout = screen.getByRole("radio", { name: /Scout/ });
    expect(within(scout).getByText("Analyst")).toBeTruthy();
    // An agent with no role gets a name and nothing else — not an empty line
    // holding the chip open.
    const nova = screen.getByRole("radio", { name: /Nova/ });
    expect(within(nova).getByText("Nova").parentElement?.children).toHaveLength(1);
  });

  it("renders nothing at all when there are no agents to choose from", () => {
    render(<AgentPicker agents={[]} selectedId={null} onSelect={() => {}} />);
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    expect(screen.getByRole("radiogroup", { name: "Choose an agent" })).toBeTruthy();
  });

  it("refuses an agent that cannot take a message, whoever handed it over", () => {
    // The chats home filters to active agents before calling this, so in
    // practice a paused one never arrives -- but the chip is holding the
    // status and should not depend on being handed a clean list. Picking a
    // paused agent can only end in a rejected send.
    const onSelect = vi.fn();
    render(
      <AgentPicker
        agents={[...AGENTS, agent({ id: "a3", name: "Pax", status: "paused" })]}
        selectedId="a1"
        onSelect={onSelect}
      />,
    );
    const paused = screen.getByRole("radio", { name: /Pax/ });
    expect((paused as HTMLButtonElement).disabled).toBe(true);
    expect(paused.textContent).toContain("paused");
    fireEvent.click(paused);
    expect(onSelect).not.toHaveBeenCalled();

    // An active one is still perfectly choosable.
    fireEvent.click(screen.getByRole("radio", { name: /Linus/ }));
    expect(onSelect).toHaveBeenCalledWith("a2");
  });

  it("draws exactly the agents it is handed", () => {
    // The picker does no filtering of its own beyond refusing the unusable:
    // whoever it is handed, it renders.
    render(
      <AgentPicker
        agents={[...AGENTS, agent({ id: "a4", name: "Pax" })]}
        selectedId="a1"
        onSelect={() => {}}
      />,
    );
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(radios.map((radio) => within(radio).getByText(/^(Scout|Linus|Pax)$/).textContent)).toEqual(
      ["Scout", "Linus", "Pax"],
    );
  });

  it("keeps every chip a comfortable tap target", () => {
    render(<AgentPicker agents={AGENTS} selectedId="a1" onSelect={() => {}} />);
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio.className).toContain("min-h-[44px]");
    }
  });
});

type PillState = Partial<Pick<Conversation, "active_task_state" | "active_run_status">>;

describe("LiveStatusPill", () => {
  const pill = (state: PillState) =>
    render(
      <LiveStatusPill
        conversation={{ active_task_state: null, active_run_status: null, ...state }}
      />,
    );

  it("renders nothing when the chat is idle", () => {
    pill({});
    expect(screen.queryByTestId("live-status")).toBeNull();
  });

  it("renders nothing for a task state that is no longer live", () => {
    pill({ active_task_state: "completed" });
    expect(screen.queryByTestId("live-status")).toBeNull();
  });

  const cases: [PillState, string, string][] = [
    [{ active_task_state: "running" }, "working", "Working…"],
    [{ active_task_state: "queued" }, "queued", "Waiting for a free slot"],
    [{ active_task_state: "paused" }, "paused", "Paused"],
    [{ active_run_status: "waiting_approval" }, "review", "Needs your review"],
    [{ active_run_status: "waiting_review" }, "waiting_review", "Waiting for a review"],
  ];

  it.each(cases)("says %o in words", (state, kind, label) => {
    // Colour is never the only signal: every state carries its own sentence.
    pill(state);
    const node = screen.getByTestId("live-status");
    expect(node.getAttribute("data-kind")).toBe(kind);
    expect(node.textContent).toContain(label);
  });

  it("puts an approval wait ahead of the task state it is blocking", () => {
    pill({ active_task_state: "running", active_run_status: "waiting_approval" });
    expect(screen.getByTestId("live-status").getAttribute("data-kind")).toBe("review");
  });

  it("animates a dot only while work is actually moving", () => {
    pill({ active_task_state: "running" });
    expect(screen.getByTestId("live-status").querySelector("[aria-hidden]")).toBeTruthy();
    cleanup();
    pill({ active_task_state: "paused" });
    expect(screen.getByTestId("live-status").querySelector("[aria-hidden]")).toBeNull();
  });

  it("clips its label instead of stretching the row it sits in", () => {
    // The pill shares the header line with the agent name and the chat title.
    pill({ active_task_state: "queued" });
    const node = screen.getByTestId("live-status");
    expect(node.className).toContain("max-w-full");
    expect(node.querySelector("span:last-child")?.className).toContain("truncate");
  });

  it("appends the caller's classes", () => {
    render(
      <LiveStatusPill
        conversation={{ active_task_state: "running", active_run_status: null }}
        className="ml-2"
      />,
    );
    expect(screen.getByTestId("live-status").className).toContain("ml-2");
  });
});
