import { describe, expect, it } from "vitest";
import { PLACEHOLDER_PARAM, placeholderParams, segmentAfter } from "@/lib/route-params";

describe("segmentAfter", () => {
  it("reads the id that follows the prefix", () => {
    expect(segmentAfter("/chats/abc123", "chats")).toBe("abc123");
    expect(segmentAfter("/chats/abc123/", "chats")).toBe("abc123");
    expect(segmentAfter("/tasks/t-1/", "tasks")).toBe("t-1");
  });

  it("treats the static-export placeholder as no id", () => {
    // The desktop shell serves this one page for every id; a view that saw
    // `_` as real would fetch a task that cannot exist.
    expect(segmentAfter(`/chats/${PLACEHOLDER_PARAM}/`, "chats")).toBe("");
  });

  it("returns empty rather than throwing on URLs that have no id", () => {
    expect(segmentAfter("/chats/", "chats")).toBe("");
    expect(segmentAfter("/chats", "chats")).toBe("");
    expect(segmentAfter("/home/", "chats")).toBe("");
    expect(segmentAfter(null, "chats")).toBe("");
    expect(segmentAfter("/chats/%E0%A4%A/", "chats")).toBe("");
  });

  it("decodes an escaped segment", () => {
    expect(segmentAfter("/invite/a%2Fb/", "invite")).toBe("a/b");
  });

  it("ignores a prefix that appears earlier in the path", () => {
    // The route group is stripped from the URL, but a workspace slug or a
    // nested path could still repeat the word.
    expect(segmentAfter("/chats/chats/", "chats")).toBe("chats");
  });

  it("gives static export exactly one page to build", () => {
    expect(placeholderParams("id")).toEqual([{ id: PLACEHOLDER_PARAM }]);
    expect(placeholderParams("token")).toEqual([{ token: PLACEHOLDER_PARAM }]);
  });
});
