/** Component tests: structured agent-message rendering (Phase 8, plan 29). */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MessageTypeBadge, StructuredMessageBody } from "@/components/task-bits";

afterEach(cleanup);

describe("MessageTypeBadge", () => {
  it("labels a delegation message", () => {
    render(<MessageTypeBadge type="delegation" content={{}} />);
    expect(screen.getByText("delegation")).toBeTruthy();
  });

  it("shows the verdict for review results", () => {
    render(<MessageTypeBadge type="review_result" content={{ verdict: "fail" }} />);
    expect(screen.getByText("review: fail")).toBeTruthy();
  });
});

describe("StructuredMessageBody", () => {
  it("renders summary, artifacts, risks, and next action", () => {
    render(
      <StructuredMessageBody
        content={{
          summary: "Opened PR #7 with the fix.",
          artifacts: [{ type: "pr", id: "7", url_ref: "http://git.example/pr/7" }],
          risks: ["migration touches prod data"],
          recommended_next_action: "delegate QA review",
        }}
      />,
    );
    expect(screen.getByText("Opened PR #7 with the fix.")).toBeTruthy();
    const link = screen.getByRole("link", { name: "pr 7" });
    expect(link.getAttribute("href")).toBe("http://git.example/pr/7");
    expect(screen.getByText("risk: migration touches prod data")).toBeTruthy();
    expect(screen.getByText("next: delegate QA review")).toBeTruthy();
  });

  it("tolerates malformed content", () => {
    render(
      <StructuredMessageBody
        content={{ artifacts: "nope", risks: [1, 2], text: "fallback text" }}
      />,
    );
    expect(screen.getByText("fallback text")).toBeTruthy();
  });
});
