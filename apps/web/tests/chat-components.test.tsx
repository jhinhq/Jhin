/** Component tests: rail items, transcript rendering, and the composer. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationRailItem } from "@/components/chat/chat-rail";
import { COMPOSER_FIELD_PAD, Composer } from "@/components/chat/composer";
import { Transcript } from "@/components/chat/transcript";
import { groupExchanges, mergeTimeline, withDaySeparators } from "@/lib/chat";
import type { ActivityCard, Conversation, ConversationMessage } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({
  useConversations: () => ({ data: { items: [], total: 0 }, isPending: false, error: null }),
}));

vi.mock("@/lib/workspace-context", () => ({
  useWorkspace: () => ({
    workspace: { workspace_id: "ws", workspace_name: "Acme", workspace_slug: "acme", role: "member" },
    user: { id: "u1", email: "a@b.c", display_name: "Ada", created_at: "" },
    role: "member",
    can: () => true,
  }),
}));

afterEach(cleanup);

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "c1",
    workspace_id: "ws",
    title: "Weekly summary",
    status: "active",
    pinned: false,
    primary_agent_id: "a1",
    created_by_user_id: "u1",
    last_activity_at: new Date().toISOString(),
    created_at: "",
    updated_at: "",
    active_task_id: null,
    active_task_state: null,
    active_run_status: null,
    active_activity: null,
    last_message_preview: "Here is what happened",
    last_message_sender_type: "agent",
    agent_name: "Scout",
    agent_role_title: "Analyst",
    task_count: 1,
    ...overrides,
  };
}

function message(overrides: Partial<ConversationMessage>): ConversationMessage {
  return {
    id: "m1",
    task_id: "t1",
    run_id: null,
    sender_type: "agent",
    sender_id: "a1",
    message_type: "text",
    content_json: { text: "hi" },
    created_at: "2026-08-21T10:00:00Z",
    conversation_id: "c1",
    sender_name: "Scout",
    agent_id: "a1",
    ...overrides,
  };
}

describe("ConversationRailItem", () => {
  it("shows the agent, title, preview, and no pill when idle", () => {
    render(
      <ul>
        <ConversationRailItem conversation={conversation()} selected={false} />
      </ul>,
    );
    expect(screen.getByText("Weekly summary")).toBeTruthy();
    expect(screen.getByText("Scout · Analyst")).toBeTruthy();
    expect(screen.getByText("Here is what happened")).toBeTruthy();
    expect(screen.queryByTestId("live-status")).toBeNull();
  });

  it.each([
    [{ active_task_state: "running" as const, active_run_status: null }, "Working…"],
    [{ active_task_state: "queued" as const, active_run_status: null }, "Waiting for a free slot"],
    [{ active_task_state: "paused" as const, active_run_status: null }, "Paused"],
    [
      { active_task_state: "running" as const, active_run_status: "waiting_approval" },
      "Needs your review",
    ],
  ])("renders the live pill for %o", (state, label) => {
    render(
      <ul>
        <ConversationRailItem conversation={conversation(state)} selected />
      </ul>,
    );
    expect(screen.getByTestId("live-status").textContent).toContain(label);
    expect(screen.getByRole("link").getAttribute("aria-current")).toBe("page");
  });
});

describe("Transcript", () => {
  it("renders user bubbles, agent bubbles, work cards, and activity chips", () => {
    const messages = [
      message({ id: "m1", sender_type: "user", content_json: { text: "Summarize the week" } }),
      message({ id: "m2", content_json: { text: "On it." }, created_at: "2026-08-21T10:01:00Z" }),
      message({
        id: "m3",
        message_type: "delegation",
        content_json: { summary: "Handing the data pull to QA.", target_agent_name: "QA" },
        created_at: "2026-08-21T10:02:00Z",
      }),
    ];
    const activity: ActivityCard[] = [
      {
        id: "task:t1:started",
        kind: "started",
        label: "Started working",
        actor_type: "agent",
        actor_agent_id: "a1",
        actor_agent_name: "Scout",
        target_agent_id: null,
        target_agent_name: null,
        task_id: "t1",
        task_title: "Summarize the week",
        root_task_id: "t1",
        conversation_id: "c1",
        approval_id: null,
        summary: "",
        detail_json: {},
        created_at: "2026-08-21T10:00:30Z",
      },
    ];
    render(
      <Transcript items={mergeTimeline(messages, activity)} agentName="Scout" userName="Ada" />,
    );
    expect(screen.getByRole("log")).toBeTruthy();
    expect(screen.getByTestId("user-message").textContent).toContain("Summarize the week");
    expect(screen.getByTestId("agent-message").textContent).toContain("On it.");
    expect(screen.getByTestId("activity-chip").textContent).toContain("Started working");

    const workCard = screen.getByTestId("work-card");
    expect(workCard.textContent).toContain("Asked QA for help");
    expect(workCard.textContent).toContain("Handing the data pull to QA.");
    expect(screen.queryByTestId("structured-message")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByTestId("structured-message")).toBeTruthy();
    expect(screen.getByText("delegation")).toBeTruthy();
  });

  it("renders nothing for an empty agent message and keeps the rest", () => {
    // Historical rows: runs that finished with no text once persisted an
    // empty agent message, which rendered as a blank bubble.
    const messages = [
      message({ id: "m1", sender_type: "user", content_json: { text: "What is Connie doing?" } }),
      message({ id: "m2", content_json: { text: "   " }, created_at: "2026-08-21T10:01:00Z" }),
    ];
    render(<Transcript items={mergeTimeline(messages, [])} agentName="Scout" userName="Ada" />);

    expect(screen.queryByTestId("agent-message")).toBeNull();
    expect(screen.getByTestId("user-message").textContent).toContain("What is Connie doing?");
  });

  it("shows the backstop note for a run that finished without a reply", () => {
    const note = message({
      id: "m3",
      sender_type: "system",
      sender_id: null,
      message_type: "note",
      content_json: { text: "Scout finished without a reply.", reason: "empty_completion" },
    });
    render(<Transcript items={mergeTimeline([note], [])} agentName="Scout" userName="Ada" />);

    expect(screen.queryByTestId("agent-message")).toBeNull();
    expect(screen.getByRole("log").textContent).toContain("Scout finished without a reply.");
  });

  it("shows the working indicator", () => {
    render(
      <Transcript
        items={[]}
        agentName="Scout"
        userName="Ada"
        liveStatus={{ label: "Working…", tone: "accent", kind: "working" }}
      />,
    );
    expect(screen.getByTestId("working-indicator").textContent).toContain("Scout is working…");
  });
});

describe("Transcript markdown", () => {
  /** Agents write markdown; before this the transcript showed the asterisks.
   * The rule the transcript follows: markdown is rendered where an agent is
   * speaking to the reader in prose, and nowhere else. */
  const reply = (text: string) =>
    mergeTimeline([message({ id: "m1", content_json: { text } })], []);

  it("formats an agent reply instead of printing its markup", () => {
    render(<Transcript items={reply("Your **CTO** runs *Engineering*.")} agentName="Scout" userName="Ada" />);
    const bubble = screen.getByTestId("agent-message");
    expect(bubble.querySelector("strong")?.textContent).toBe("CTO");
    expect(bubble.querySelector("em")?.textContent).toBe("Engineering");
    expect(bubble.textContent).not.toContain("**");
  });

  it("renders lists, headings, and inline code in a reply", () => {
    render(
      <Transcript
        items={reply("## Team\n\n- Ada — `CTO`\n- Ben — `CFO`")}
        agentName="Scout"
        userName="Ada"
      />,
    );
    const bubble = screen.getByTestId("agent-message");
    expect(bubble.querySelector("h3")?.textContent).toBe("Team");
    expect(bubble.querySelectorAll("li")).toHaveLength(2);
    expect(bubble.querySelectorAll("code")).toHaveLength(2);
  });

  it("scrolls a long code block inside the bubble instead of widening it", () => {
    const long = "x".repeat(400);
    render(<Transcript items={reply("```sh\n" + long + "\n```")} agentName="Scout" userName="Ada" />);
    const bubble = screen.getByTestId("agent-message");
    const pre = bubble.querySelector("pre")!;
    expect(pre.textContent).toContain(long);
    // The contract that keeps the layout: the block scrolls, and the bubble
    // may shrink below its content so `max-w` still binds.
    expect(pre.className).toContain("overflow-x-auto");
    const column = bubble.children[1];
    expect(column.className).toContain("min-w-0");
    expect(column.className).toContain("max-w-[min(85%,40rem)]");
  });

  it("never turns a javascript: link in a reply into an anchor", () => {
    render(
      <Transcript
        items={reply("Read [this](javascript:alert(1)) and [that](java\nscript:alert(1))")}
        agentName="Scout"
        userName="Ada"
      />,
    );
    const bubble = screen.getByTestId("agent-message");
    expect(bubble.querySelector("a")).toBeNull();
    expect(bubble.textContent).toContain("javascript:alert(1)");
  });

  it("renders a safe link in a reply as a new-tab anchor", () => {
    render(<Transcript items={reply("See [docs](https://jhin.dev).")} agentName="Scout" userName="Ada" />);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link.getAttribute("href")).toBe("https://jhin.dev");
    expect(link.getAttribute("rel")).toBe("noreferrer noopener");
  });

  it("shows markup a person typed exactly as they typed it", () => {
    // A typed message is quoted, not formatted: there is no way to escape a
    // character from the composer, so `**not bold**` has to stay literal.
    const items = mergeTimeline(
      [
        message({
          id: "m1",
          sender_type: "user",
          content_json: { text: "why is **not bold** in my_file_name?" },
        }),
      ],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const bubble = screen.getByTestId("user-message");
    expect(bubble.querySelector("strong")).toBeNull();
    expect(bubble.textContent).toContain("**not bold**");
  });

  it("leaves the clamped work-card summary literal too", () => {
    const items = mergeTimeline(
      [
        message({
          id: "m2",
          message_type: "delegation",
          content_json: { summary: "Handing **the data pull** to QA.", target_agent_name: "QA" },
        }),
      ],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const card = screen.getByTestId("work-card");
    expect(card.querySelector("strong")).toBeNull();
    expect(card.textContent).toContain("**the data pull**");
  });

  it("still renders nothing for an agent message that is only markup whitespace", () => {
    render(<Transcript items={reply("   ")} agentName="Scout" userName="Ada" />);
    expect(screen.queryByTestId("agent-message")).toBeNull();
  });
});

describe("Transcript quiet exchanges", () => {
  const delegation = message({
    id: "d1",
    message_type: "delegation",
    content_json: { summary: "Handing this off", target_agent_id: "a2", target_agent_name: "Linus" },
    created_at: "2026-08-21T10:00:00Z",
  });
  const reported = message({
    id: "r1",
    message_type: "result",
    sender_id: "a2",
    agent_id: "a2",
    sender_name: "Linus",
    content_json: { summary: "All done", from_agent_id: "a2", from_agent_name: "Linus" },
    created_at: "2026-08-21T10:05:00Z",
  });
  const grouping = { primaryAgentId: "a1", primaryAgentName: "Scout" };
  const items = groupExchanges(mergeTimeline([delegation, reported], []), grouping);

  it("collapses agent↔agent runs into a quiet row that expands inline", () => {
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const toggle = screen.getByRole("button", { name: /2 updates with Linus/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("work-card")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByTestId("work-card")).toHaveLength(2);

    fireEvent.click(toggle);
    expect(screen.queryByTestId("work-card")).toBeNull();
  });

  it("expands exchanges by default when the detailed toggle is on", () => {
    render(<Transcript items={items} agentName="Scout" userName="Ada" expandExchanges />);
    expect(screen.getAllByTestId("work-card")).toHaveLength(2);
  });

  it("appends an outcome suffix when the exchange ran into a problem", () => {
    const escalated = message({
      id: "e1",
      message_type: "escalation",
      sender_id: "a2",
      agent_id: "a2",
      sender_name: "Linus",
      content_json: { summary: "Stuck on credentials", from_agent_id: "a2", from_agent_name: "Linus" },
      created_at: "2026-08-21T10:06:00Z",
    });
    const failedItems = groupExchanges(mergeTimeline([delegation, escalated], []), grouping);
    render(<Transcript items={failedItems} agentName="Scout" userName="Ada" />);
    expect(screen.getByRole("button", { name: /ran into a problem/ })).toBeTruthy();
  });

  it("renders centered date separators when the day changes", () => {
    // Local-component dates so the labels hold in any timezone.
    const now = new Date(2026, 7, 21, 12, 0);
    const older = message({
      id: "m-old",
      content_json: { text: "From yesterday" },
      created_at: new Date(2026, 7, 20, 9, 0).toISOString(),
    });
    const today = message({
      id: "m-new",
      content_json: { text: "From today" },
      created_at: new Date(2026, 7, 21, 8, 30).toISOString(),
    });
    const dayItems = withDaySeparators(mergeTimeline([older, today], []), now);
    render(<Transcript items={dayItems} agentName="Scout" userName="Ada" />);
    const separators = screen.getAllByTestId("day-separator");
    expect(separators).toHaveLength(2);
    expect(separators[0].textContent).toContain("Yesterday");
    expect(separators[1].textContent).toContain("Today");
  });
});

describe("Transcript queued instructions", () => {
  it("shows a queued pill for an instruction with no later activity yet", () => {
    const items = mergeTimeline(
      [
        message({
          id: "u1",
          sender_type: "user",
          message_type: "instruction",
          content_json: { text: "Also check staging" },
          created_at: "2026-08-21T10:05:00Z",
          task_id: "t1",
        }),
      ],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const status = screen.getByTestId("instruction-status");
    expect(status.getAttribute("data-state")).toBe("queued");
    expect(status.textContent).toContain("Queued");
    expect(status.textContent).toContain("Scout");
  });

  it("swaps to a delivered note once a later agent message on the same task appears", () => {
    const items = mergeTimeline(
      [
        message({
          id: "u1",
          sender_type: "user",
          message_type: "instruction",
          content_json: { text: "Also check staging" },
          created_at: "2026-08-21T10:05:00Z",
          task_id: "t1",
        }),
        message({
          id: "a1",
          sender_type: "agent",
          message_type: "text",
          content_json: { text: "Checked staging, looks fine." },
          created_at: "2026-08-21T10:06:00Z",
          task_id: "t1",
        }),
      ],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const status = screen.getByTestId("instruction-status");
    expect(status.getAttribute("data-state")).toBe("delivered");
    expect(status.textContent).toContain("Steered Scout");
  });

  it("resolves two back-to-back queued instructions independently", () => {
    const items = mergeTimeline(
      [
        message({
          id: "u1",
          sender_type: "user",
          message_type: "instruction",
          content_json: { text: "First" },
          created_at: "2026-08-21T10:05:00Z",
          task_id: "t1",
        }),
        message({
          id: "a1",
          sender_type: "agent",
          message_type: "text",
          content_json: { text: "Working on it" },
          created_at: "2026-08-21T10:06:00Z",
          task_id: "t1",
        }),
        message({
          id: "u2",
          sender_type: "user",
          message_type: "instruction",
          content_json: { text: "Second" },
          created_at: "2026-08-21T10:07:00Z",
          task_id: "t1",
        }),
      ],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    const statuses = screen.getAllByTestId("instruction-status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0].getAttribute("data-state")).toBe("delivered");
    expect(statuses[1].getAttribute("data-state")).toBe("queued");
  });

  it("does not render an instruction-status pill for ordinary user messages", () => {
    const items = mergeTimeline(
      [message({ id: "u1", sender_type: "user", content_json: { text: "hi" } })],
      [],
    );
    render(<Transcript items={items} agentName="Scout" userName="Ada" />);
    expect(screen.queryByTestId("instruction-status")).toBeNull();
  });
});

describe("Composer", () => {
  it("sends on Enter and inserts a newline on Shift+Enter", () => {
    const onSend = vi.fn();
    const onChange = vi.fn();
    render(<Composer value="hello" onChange={onChange} onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: "Message" });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does not send when empty or disabled and explains why", () => {
    const onSend = vi.fn();
    const { rerender } = render(<Composer value="   " onChange={() => {}} onSend={onSend} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement).disabled).toBe(true);

    rerender(
      <Composer
        value="hello"
        onChange={() => {}}
        onSend={onSend}
        disabled
        disabledReason="This chat is archived."
      />,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getAllByText("This chat is archived.").length).toBeGreaterThan(0);
  });

  it("hides the Stop button when nothing is active", () => {
    render(<Composer value="" onChange={() => {}} onSend={() => {}} />);
    expect(screen.queryByTestId("composer-stop")).toBeNull();
  });

  it("shows a Stop button while a task is active and calls onStop when clicked", () => {
    const onStop = vi.fn();
    render(
      <Composer
        value=""
        onChange={() => {}}
        onSend={() => {}}
        canStop
        onStop={onStop}
        stopLabel="Stop Scout"
      />,
    );
    const stop = screen.getByTestId("composer-stop");
    expect(stop.getAttribute("aria-label")).toBe("Stop Scout");
    expect((stop as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("puts the settings chips on the controls row, ahead of Stop and Send", () => {
    render(
      <Composer
        value=""
        onChange={() => {}}
        onSend={() => {}}
        canStop
        onStop={() => {}}
        controls={<button type="button">Model</button>}
      />,
    );
    const controls = screen.getByTestId("composer-controls");
    const chip = screen.getByRole("button", { name: "Model" });
    const send = screen.getByRole("button", { name: "Send message" });
    expect(controls.contains(chip)).toBe(true);
    expect(chip.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("disables the Stop button while a stop/pause/resume request is in flight", () => {
    render(
      <Composer value="" onChange={() => {}} onSend={() => {}} canStop stopping onStop={() => {}} />,
    );
    expect((screen.getByTestId("composer-stop") as HTMLButtonElement).disabled).toBe(true);
  });
});

/* The text field must read as centred inside the rounded shell: its left
 * inset and its right inset are both just its own horizontal padding, and
 * that padding cannot depend on how many trailing controls are visible. The
 * mechanism is structural — the controls live on their own row instead of
 * being flex siblings that eat into the field's width. */
describe("Composer text field centring", () => {
  /** Every horizontal-padding utility on the element, in class order. */
  function horizontalPadding(node: Element): string[] {
    return Array.from(node.classList).filter((name) =>
      /^(px|pl|pr|ps|pe)-\[?[\d.]/.test(name),
    );
  }

  function field(): HTMLTextAreaElement {
    return screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  }

  const configurations: [string, React.ReactElement][] = [
    [
      "no trailing controls beyond Send",
      <Composer key="a" value="" onChange={() => {}} onSend={() => {}} />,
    ],
    [
      "Send enabled",
      <Composer key="b" value="hello" onChange={() => {}} onSend={() => {}} />,
    ],
    [
      "Send + Stop",
      <Composer key="c" value="hello" onChange={() => {}} onSend={() => {}} canStop onStop={() => {}} />,
    ],
    [
      "disabled with a reason",
      <Composer key="d" value="" onChange={() => {}} onSend={() => {}} disabled disabledReason="Archived." />,
    ],
  ];

  it.each(configurations)("keeps symmetric horizontal padding: %s", (_name, element) => {
    render(element);
    // Exactly one symmetric `px-*`; no one-sided pl/pr/ps/pe can creep in.
    expect(horizontalPadding(field())).toEqual(["px-4"]);
  });

  it("uses the same inset for every configuration and matches the exported contract", () => {
    const insets = configurations.map(([, element]) => {
      const view = render(element);
      const value = horizontalPadding(field()).join(" ");
      view.unmount();
      return value;
    });
    expect(new Set(insets).size).toBe(1);
    expect(COMPOSER_FIELD_PAD.docked.split(" ")).toContain(insets[0]);
  });

  it("uses the large variant's symmetric inset on the home hero", () => {
    render(<Composer variant="large" value="" onChange={() => {}} onSend={() => {}} />);
    expect(horizontalPadding(field())).toEqual(["px-5"]);
    expect(COMPOSER_FIELD_PAD.large.split(" ")).toContain("px-5");
  });

  it("keeps the trailing controls out of the text field's row", () => {
    render(
      <Composer value="hi" onChange={() => {}} onSend={() => {}} canStop onStop={() => {}} />,
    );
    const label = field().parentElement as HTMLElement;
    const controls = screen.getByTestId("composer-controls");
    const send = screen.getByRole("button", { name: "Send message" });
    const stop = screen.getByTestId("composer-stop");

    // Both controls sit in the controls row, and that row is a *sibling* of
    // the label rather than sharing a flex line with the text field.
    expect(controls.contains(send)).toBe(true);
    expect(controls.contains(stop)).toBe(true);
    expect(label.contains(send)).toBe(false);
    expect(controls.parentElement).toBe(label.parentElement);
    expect(screen.getByTestId("composer-shell").className).toContain("flex-col");
  });
});
