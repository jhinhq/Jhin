/** Unit tests: the turn envelope every send builds, the draft handed across
 * the new-chat redirect, and the constants the /chats surfaces persist under. */

import { describe, expect, it } from "vitest";
import {
  CHAT_DETAILED_STORAGE_KEY,
  LAST_AGENT_STORAGE_KEY,
  STARTER_PROMPTS,
  messageText,
  newTurn,
  stashCarriedDraft,
  takeCarriedDraft,
} from "@/lib/chat";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

describe("newTurn", () => {
  it("trims the text and stamps a uuid", () => {
    const turn = newTurn("  Summarize the week  ");
    expect(turn.text).toBe("Summarize the week");
    expect(turn.client_turn_id).toMatch(UUID);
    expect(Object.keys(turn).sort()).toEqual(["client_turn_id", "text"]);
  });

  it("mints a fresh id per call", () => {
    // The whole idempotency contract rests on this: the server dedupes on
    // client_turn_id, so a reused id would silently swallow the second turn.
    const ids = new Set(Array.from({ length: 200 }, () => newTurn("same text").client_turn_id));
    expect(ids.size).toBe(200);
  });

  it("still stamps an id for text that trims to nothing", () => {
    // Callers guard against empty sends; the envelope stays well-formed
    // regardless so a retry can never be mistaken for a fresh turn.
    const turn = newTurn("   \n  ");
    expect(turn.text).toBe("");
    expect(turn.client_turn_id).toMatch(UUID);
  });
});

describe("carried draft", () => {
  it("round-trips a draft for one conversation", () => {
    stashCarriedDraft("c1", "half a thought");
    expect(takeCarriedDraft("c1")).toBe("half a thought");
  });

  it("is consumed by the first read", () => {
    // The conversation page seeds its composer once on mount; a draft that
    // survived would reappear every time the page remounted.
    stashCarriedDraft("c2", "only once");
    expect(takeCarriedDraft("c2")).toBe("only once");
    expect(takeCarriedDraft("c2")).toBe("");
  });

  it("returns an empty string when nothing was stashed", () => {
    expect(takeCarriedDraft("never-written")).toBe("");
  });

  it("keeps conversations apart", () => {
    stashCarriedDraft("c3", "for three");
    stashCarriedDraft("c4", "for four");
    expect(takeCarriedDraft("c4")).toBe("for four");
    expect(takeCarriedDraft("c3")).toBe("for three");
  });

  it("stashes nothing for a whitespace-only draft", () => {
    stashCarriedDraft("c5", "   ");
    expect(takeCarriedDraft("c5")).toBe("");
  });

  it("preserves the draft verbatim, including its own whitespace", () => {
    stashCarriedDraft("c6", "  line one\n\nline two  ");
    expect(takeCarriedDraft("c6")).toBe("  line one\n\nline two  ");
  });

  it("degrades quietly when sessionStorage throws", () => {
    // Safari private mode throws from the accessor itself. Losing the
    // carry-over is acceptable; taking the page down with it is not.
    const original = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    const deny = () => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    };
    Object.defineProperty(window, "sessionStorage", {
      value: { getItem: deny, setItem: deny, removeItem: deny },
      configurable: true,
      writable: true,
    });
    try {
      expect(() => stashCarriedDraft("c7", "lost but harmless")).not.toThrow();
      expect(takeCarriedDraft("c7")).toBe("");
    } finally {
      if (original) Object.defineProperty(window, "sessionStorage", original);
    }
  });
});

describe("messageText", () => {
  it("prefers text, then summary, then content", () => {
    expect(messageText({ content_json: { text: "a", summary: "b", content: "c" } })).toBe("a");
    expect(messageText({ content_json: { summary: "b", content: "c" } })).toBe("b");
    expect(messageText({ content_json: { content: "c" } })).toBe("c");
  });

  it("skips empty and non-string values instead of rendering them", () => {
    // content_json is whatever the backend wrote; a stray object here used to
    // reach the bubble as "[object Object]".
    expect(messageText({ content_json: { text: "", summary: "b" } })).toBe("b");
    expect(messageText({ content_json: { text: { nested: 1 }, summary: "b" } })).toBe("b");
    expect(messageText({ content_json: {} })).toBe("");
  });
});

describe("chat constants", () => {
  it("gives every starter prompt a distinct, non-empty line", () => {
    // The chats home renders these with `key={prompt}`, so a duplicate would
    // collide as a React key.
    expect(STARTER_PROMPTS.length).toBeGreaterThan(0);
    expect(new Set(STARTER_PROMPTS).size).toBe(STARTER_PROMPTS.length);
    for (const prompt of STARTER_PROMPTS) expect(prompt.trim()).toBe(prompt);
  });

  it("namespaces the two persisted keys and keeps them apart", () => {
    // These name real browser storage: renaming one silently drops what
    // people already have, and a collision would make the detailed toggle and
    // the remembered agent overwrite each other.
    expect(LAST_AGENT_STORAGE_KEY).toBe("jhin-last-agent");
    expect(CHAT_DETAILED_STORAGE_KEY).toBe("jhin-chat-detailed");
    expect(LAST_AGENT_STORAGE_KEY).not.toBe(CHAT_DETAILED_STORAGE_KEY);
  });
});
