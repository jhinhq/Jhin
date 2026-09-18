/**
 * The receipt for a name an agent actually changed.
 *
 * The bug behind this surface was an agent saying "you can call me Bisby for
 * this chat" while nothing changed at all. Now that a name really is the agent
 * row, the opposite risk appears — a change to how an agent is identified
 * everywhere, made mid-conversation, that a person could scroll past — so the
 * assertions here are about the change being visible and legible: what it was,
 * what it is, and the handle that did not move.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RenameCard } from "@/components/chat/rename-card";
import { Transcript } from "@/components/chat/transcript";
import {
  friendlyMessageLabel,
  isWorkCard,
  mergeTimeline,
  readAgentRenamed,
} from "@/lib/chat";
import type { ConversationMessage } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({}));

afterEach(cleanup);

function renameMessage(content: Record<string, unknown> = {}): ConversationMessage {
  return {
    id: "m-rename",
    task_id: "t1",
    run_id: "r1",
    sender_type: "agent",
    sender_id: "a1",
    message_type: "status",
    created_at: "2026-09-06T10:05:00.000Z",
    conversation_id: "c1",
    sender_name: "Bisby",
    agent_id: "a1",
    content_json: {
      kind: "agent_renamed",
      previous_name: "Senior Software Engineer",
      name: "Bisby",
      slug: "senior-software-engineer",
      ...content,
    },
  };
}

describe("readAgentRenamed", () => {
  it("reads the old name, the new one, and the unchanged handle", () => {
    expect(readAgentRenamed(renameMessage())).toEqual({
      kind: "agent_renamed",
      previous_name: "Senior Software Engineer",
      name: "Bisby",
      slug: "senior-software-engineer",
    });
  });

  it("is null for anything that is not this receipt", () => {
    expect(readAgentRenamed({ ...renameMessage(), content_json: {} })).toBeNull();
    expect(
      readAgentRenamed({ ...renameMessage(), content_json: { kind: "memory_saved" } }),
    ).toBeNull();
  });

  it("is null for a receipt a person could have written", () => {
    // The card is evidence, so sender and type are checked as well as the key.
    const content = { kind: "agent_renamed", name: "Bisby" };
    expect(
      readAgentRenamed({ sender_type: "user", message_type: "status", content_json: content }),
    ).toBeNull();
    expect(
      readAgentRenamed({ sender_type: "agent", message_type: "text", content_json: content }),
    ).toBeNull();
  });

  it("is null when the payload cannot say what the agent is called now", () => {
    expect(readAgentRenamed(renameMessage({ name: "  " }))).toBeNull();
  });
});

describe("RenameCard", () => {
  it("shows the new name and the one it replaced", () => {
    render(<RenameCard message={renameMessage()} name="Bisby" />);
    const card = screen.getByTestId("rename-card");
    expect(card.textContent).toContain("Now called Bisby");
    expect(card.textContent).toContain("was Senior Software Engineer");
  });

  it("says the handle did not move, because that is the question a rename raises", () => {
    render(<RenameCard message={renameMessage()} name="Bisby" />);
    const slug = screen.getByTestId("rename-slug");
    expect(slug.textContent).toContain("senior-software-engineer");
    expect(slug.textContent).toContain("existing links still work");
  });

  it("links to the agent so a person can put it back", () => {
    render(<RenameCard message={renameMessage()} name="Bisby" />);
    expect(
      screen.getByRole("link", { name: /review or change this/i }).getAttribute("href"),
    ).toBe("/agents/a1");
  });

  it("renders nothing rather than an empty claim", () => {
    const { container } = render(
      <RenameCard message={renameMessage({ name: "" })} name="Bisby" />,
    );
    expect(container.innerHTML).toBe("");
  });
});

describe("a rename receipt in the transcript", () => {
  it("renders as its own card, not as a generic work card", () => {
    const message = renameMessage();
    expect(isWorkCard(message)).toBe(false);
    expect(friendlyMessageLabel(message)).toBe("Now called Bisby");

    render(<Transcript items={mergeTimeline([message], [])} agentName="Bisby" userName="Varand" />);
    expect(screen.getByTestId("rename-card")).toBeTruthy();
    expect(screen.queryByTestId("work-card")).toBeNull();
    expect(document.body.textContent).not.toContain("Shared an update");
  });
});
