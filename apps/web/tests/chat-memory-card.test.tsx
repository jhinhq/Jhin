/**
 * The receipt for a memory the agent actually wrote: the stored words, who
 * they are stored for, what they replaced, and a way to go change them.
 *
 * The bug these cover is an agent's prose being the only evidence a write
 * happened — including one "saved company-wide" that had landed on a single
 * agent — so the assertions are about the card showing the *stored* audience
 * and the *stored* words, not a restatement of either.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryCard } from "@/components/chat/memory-card";
import { Transcript } from "@/components/chat/transcript";
import {
  friendlyMessageLabel,
  groupExchanges,
  isWorkCard,
  mergeTimeline,
  readMemorySaved,
} from "@/lib/chat";
import type { ConversationMessage } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({}));

afterEach(cleanup);

const REMEMBERED = "Deployments go out on Mondays at 9:00 AM Pacific Time.";

function memoryMessage(content: Record<string, unknown> = {}): ConversationMessage {
  return {
    id: "m-memory",
    task_id: "t1",
    run_id: "r1",
    sender_type: "agent",
    sender_id: "a1",
    message_type: "status",
    created_at: "2026-08-27T10:05:00.000Z",
    conversation_id: "c1",
    sender_name: "Bisby",
    agent_id: "a1",
    content_json: {
      kind: "memory_saved",
      memory_id: "mem-9f3c",
      action: "saved",
      scope: "workspace",
      scope_label: "everyone in the workspace",
      content: REMEMBERED,
      superseded: "",
      ...content,
    },
  };
}

describe("readMemorySaved", () => {
  it("reads the stored words, the action, and the audience", () => {
    expect(readMemorySaved(memoryMessage())).toEqual({
      kind: "memory_saved",
      memory_id: "mem-9f3c",
      action: "saved",
      scope: "workspace",
      scope_label: "everyone in the workspace",
      content: REMEMBERED,
      superseded: "",
      still_standing: "",
    });
  });

  it("is not a memory receipt when the message is something else", () => {
    const agentStatus = { sender_type: "agent", message_type: "status" } as const;
    expect(readMemorySaved({ ...agentStatus, content_json: { kind: "work_request" } })).toBeNull();
    expect(readMemorySaved({ ...agentStatus, content_json: {} })).toBeNull();
  });

  it("refuses a receipt that did not come from an agent", () => {
    // The one card whose whole purpose is to be evidence rather than a claim.
    // Nothing can set content_json through the API today; the guard belongs
    // here anyway, on the card that would be worth forging.
    const content = { kind: "memory_saved", scope: "workspace", content: "anything" };
    expect(
      readMemorySaved({ sender_type: "user", message_type: "status", content_json: content }),
    ).toBeNull();
    expect(
      readMemorySaved({ sender_type: "agent", message_type: "text", content_json: content }),
    ).toBeNull();
  });

  it("falls back to a vague audience rather than naming a team it wasn't told", () => {
    const read = readMemorySaved(memoryMessage({ scope: "team", scope_label: "" }));
    expect(read?.scope_label).toBe("your team");
    // Never a team name: the platform owns those, and inventing one here is
    // the mislabelling this card exists to catch.
    expect(read?.scope_label).not.toMatch(/Platform|Engineering/);
  });

  it("keeps an unrecognized scope out of the deep link instead of guessing", () => {
    const read = readMemorySaved(memoryMessage({ scope: "everyone", scope_label: "" }));
    expect(read?.scope).toBeNull();
    expect(read?.scope_label).toBe("this agent");
  });

  it("treats anything but an explicit update as a plain save", () => {
    expect(readMemorySaved(memoryMessage({ action: "" }))?.action).toBe("saved");
    expect(readMemorySaved(memoryMessage({ action: "updated" }))?.action).toBe("updated");
  });
});

describe("MemoryCard", () => {
  it("shows what was stored and who it is stored for", () => {
    render(<MemoryCard message={memoryMessage()} name="Bisby" />);
    const card = screen.getByTestId("memory-card");
    expect(card.getAttribute("data-action")).toBe("saved");
    expect(card.textContent).toContain("Remembered");
    expect(card.textContent).toContain("for everyone in the workspace");
    expect(screen.getByTestId("memory-content").textContent).toBe(REMEMBERED);
    // The id is plumbing; the words and the audience are the receipt.
    expect(card.textContent).not.toContain("mem-9f3c");
  });

  it("makes an update legible by showing what it replaced", () => {
    render(
      <MemoryCard
        message={memoryMessage({
          action: "updated",
          scope: "team",
          scope_label: "the Platform team",
          superseded: "Deployments go out on Fridays at 4:00 PM Pacific Time.",
        })}
        name="Bisby"
      />,
    );
    expect(screen.getByTestId("memory-card").textContent).toContain("Memory updated");
    expect(screen.getByTestId("memory-content").textContent).toBe(REMEMBERED);
    const replaced = screen.getByTestId("memory-superseded");
    expect(replaced.textContent).toContain("Replaced:");
    expect(replaced.textContent).toContain("Fridays at 4:00 PM");
  });

  it("links to this agent's memory at the scope the record lives in", () => {
    render(<MemoryCard message={memoryMessage()} name="Bisby" />);
    const link = screen.getByRole("link", { name: /review or change this/i });
    expect(link.getAttribute("href")).toBe("/agents/a1?tab=memory&memory_scope=workspace");
  });

  it("drops the link rather than pointing somewhere else when the sender is unknown", () => {
    const orphan = { ...memoryMessage(), agent_id: null };
    render(<MemoryCard message={orphan} name="Bisby" />);
    expect(screen.queryByRole("link", { name: /review or change this/i })).toBeNull();
    // The receipt itself still stands.
    expect(screen.getByTestId("memory-content").textContent).toBe(REMEMBERED);
  });

  it("folds a long memory behind a button instead of truncating it away", () => {
    const long = `${"Ship on Mondays. ".repeat(40)}Never on a Friday.`;
    render(<MemoryCard message={memoryMessage({ content: long })} name="Bisby" />);
    const shown = () => screen.getByTestId("memory-content").textContent ?? "";
    expect(shown()).not.toContain("Never on a Friday.");
    fireEvent.click(screen.getByRole("button", { name: "Show all of it" }));
    expect(shown()).toBe(long);
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(shown()).not.toBe(long);
  });

  it("renders nothing rather than an empty claim when the words are missing", () => {
    // A card reading "Remembered" over nothing is the same unbacked assertion
    // the card was built to replace.
    const { container } = render(<MemoryCard message={memoryMessage({ content: "" })} name="Bisby" />);
    expect(container.innerHTML).toBe("");
  });
});

describe("a memory receipt in the transcript", () => {
  it("renders as its own card, not as a generic work card", () => {
    const message = memoryMessage();
    expect(isWorkCard(message)).toBe(false);
    expect(friendlyMessageLabel(message)).toBe("Remembered something");
    expect(friendlyMessageLabel(memoryMessage({ action: "updated" }))).toBe("Updated a memory");

    render(<Transcript items={mergeTimeline([message], [])} agentName="Bisby" userName="Varand" />);
    expect(screen.getByTestId("memory-card")).toBeTruthy();
    expect(screen.queryByTestId("work-card")).toBeNull();
    expect(document.body.textContent).not.toContain("Shared an update");
  });

  it("stays in the open, even when a colleague wrote it mid-delegation", () => {
    // Folded into a collapsed exchange, the one moment a person could catch a
    // wrong memory would be hidden behind a disclosure triangle.
    const colleagueMemory: ConversationMessage = {
      ...memoryMessage({ scope: "agent", scope_label: "just you and me" }),
      sender_id: "a2",
      agent_id: "a2",
      sender_name: "Linus",
    };
    const items = groupExchanges(mergeTimeline([colleagueMemory], []), {
      primaryAgentId: "a1",
      primaryAgentName: "Bisby",
    });
    expect(items.every((item) => item.kind === "message")).toBe(true);

    render(<Transcript items={items} agentName="Bisby" userName="Varand" />);
    expect(screen.getByTestId("memory-card").textContent).toContain("for just you and me");
    expect(screen.queryByTestId("exchange")).toBeNull();
  });
});

describe("a correction that did not replace anything", () => {
  it("says the older memory is still live, because the agent will recall both", () => {
    // Live: "the architecture review moved from Wednesday 2pm to Thursday
    // 11am" stored a second record and left the first active. The card said
    // "Remembered" and nothing else, while the agent's own message in the
    // same transcript claimed it had replaced the Wednesday one.
    render(
      <MemoryCard
        message={memoryMessage({
          action: "saved",
          scope: "team",
          scope_label: "the Engineering team",
          content: "Architecture review is Thursday 11am Pacific.",
          superseded: "",
          still_standing: "Architecture review is Wednesday 2pm Pacific.",
        })}
        name="Bisby"
      />,
    );
    const warning = screen.getByTestId("memory-still-standing");
    expect(warning.textContent).toContain("Wednesday 2pm");
    expect(warning.textContent).toContain("Still remembered too");
  });

  it("says nothing when the correction did replace the old one", () => {
    render(
      <MemoryCard
        message={memoryMessage({
          action: "updated",
          scope: "team",
          scope_label: "the Engineering team",
          content: "Retention is 24 hours.",
          superseded: "Retention is 48 hours.",
          still_standing: "",
        })}
        name="Bisby"
      />,
    );
    expect(screen.queryByTestId("memory-still-standing")).toBeNull();
    expect(screen.getByTestId("memory-superseded").textContent).toContain("48 hours");
  });
});
