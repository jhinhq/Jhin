/**
 * The failure card: what a person reads when a turn didn't finish, and what
 * they can do about it.
 *
 * The run that prompted this said, verbatim, in a chat: "Run failed: tool
 * call a34dd1dc-ba6e-506e-a509-167d07efc02d execution outcome is unknown;
 * manual reconciliation is required" — with no control of any kind under it.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FailureCard } from "@/components/chat/failure-card";
import { Transcript } from "@/components/chat/transcript";
import { mergeTimeline, readFailure } from "@/lib/chat";
import type { ConversationMessage, ConversationResume, FailureNotice } from "@/lib/types";

vi.mock("@/lib/workspace-context", () => ({
  useWorkspace: () => ({
    workspace: { workspace_id: "ws", workspace_name: "Acme", workspace_slug: "acme", role: "member" },
    user: { id: "u1", email: "a@b.c", display_name: "Ada", created_at: "" },
    role: "member",
    can: () => true,
  }),
}));

afterEach(cleanup);

const REFERENCE = "a34dd1dc-ba6e-506e-a509-167d07efc02d";
const RAW = `Run failed: tool call ${REFERENCE} execution outcome is unknown; manual reconciliation is required`;

function notice(overrides: Partial<FailureNotice> = {}): FailureNotice {
  return {
    code: "tool_execution_unknown",
    summary:
      "A step was cut short before it could report back, so there is no record of whether it finished.",
    detail: "",
    reference: REFERENCE,
    ...overrides,
  };
}

function failureMessage(overrides: Partial<ConversationMessage> = {}): ConversationMessage {
  return {
    id: "m-fail",
    task_id: "t1",
    run_id: "r1",
    sender_type: "system",
    sender_id: null,
    message_type: "error",
    content_json: { text: RAW, error_code: "tool_execution_unknown" },
    created_at: "2026-09-05T00:26:02Z",
    conversation_id: "c1",
    sender_name: "System",
    agent_id: null,
    failure: notice(),
    ...overrides,
  };
}

function offer(overrides: Partial<ConversationResume> = {}): ConversationResume {
  return {
    task_id: "t1",
    run_id: "r1",
    state: "ready",
    reason: "Bisby can pick this up from your message — there's nothing to retype.",
    instruction: "list the top level files",
    unreconciled_tool_call_id: null,
    ...overrides,
  };
}

describe("readFailure", () => {
  it("prefers the notice the API wrote", () => {
    expect(readFailure(failureMessage())?.summary).toContain("A step was cut short");
  });

  it("never puts the row's own text on screen when the notice is missing", () => {
    // Two ways to meet a row with no notice: a message cached from before the
    // deploy, and a new web served by an API older than the field — which is
    // `docs/deployment.md` step 6 (api, then web) done in the wrong order.
    // Neither is allowed to cost a person a screenful of somebody else's
    // vocabulary, so the ordering can only cost detail, never regression.
    const stale = readFailure(failureMessage({ failure: undefined }));

    expect(stale?.summary).toBe("The run stopped before it finished.");
    expect(stale?.summary).not.toContain("execution outcome is unknown");
    expect(stale?.summary).not.toContain("Run failed");
    expect(stale?.detail).toBe("");
    // The code is a fixed vocabulary rather than prose, and it is what keeps
    // the specific cure ("out of credit" → Models) on a stale card.
    expect(stale?.code).toBe("tool_execution_unknown");
  });

  it("shows nothing of the raw text even when the whole card renders stale", () => {
    render(<FailureCard message={failureMessage({ failure: undefined })} agentName="Bisby" />);

    const card = screen.getByTestId("failure-card");
    expect(card.textContent).toContain("Bisby couldn't finish that");
    expect(card.textContent).toContain("The run stopped before it finished.");
    expect(card.textContent).not.toContain("manual reconciliation");
    expect(card.textContent).not.toContain("tool call");
  });

  it("is nothing at all for a message that is not a failure", () => {
    expect(readFailure(failureMessage({ message_type: "note" }))).toBeNull();
    expect(readFailure(failureMessage({ sender_type: "agent" }))).toBeNull();
  });
});

describe("FailureCard", () => {
  it("leads with the product's sentence and keeps the identifier for support", () => {
    render(<FailureCard message={failureMessage()} agentName="Bisby" />);

    const card = screen.getByTestId("failure-card");
    expect(card.textContent).toContain("Bisby couldn't finish that");
    expect(card.textContent).toContain("A step was cut short before it could report back");
    // The three things that made the original unreadable are gone from the
    // prose: the internal noun phrase, the operator instruction, and the id
    // opening the line.
    expect(card.textContent).not.toContain("manual reconciliation");
    expect(card.textContent).not.toContain("Run failed:");
    expect(card.textContent?.indexOf(REFERENCE)).toBeGreaterThan(
      card.textContent?.indexOf("A step was cut short") ?? 0,
    );
    expect(screen.getByTestId("failure-reference").textContent).toContain(REFERENCE);
  });

  it("offers one press that sends the same message again", () => {
    const onRetry = vi.fn();
    render(
      <FailureCard
        message={failureMessage()}
        agentName="Bisby"
        resume={offer()}
        canAct
        onRetry={onRetry}
      />,
    );

    fireEvent.click(screen.getByTestId("retry-turn"));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("failure-next-step").dataset.state).toBe("ready");
  });

  it("says what pressing does before offering the press, in every state", () => {
    // One shape for all three: a rule, the sentence, then the control if
    // there is one. Read the other way round — button first, explanation
    // underneath in the faintest text on the card — the reassurance that
    // nothing has to be retyped arrives after the decision it was for.
    const reasonThenControl = (resume: ConversationResume, control: string | null) => {
      const view = render(
        <FailureCard
          message={failureMessage()}
          agentName="Bisby"
          resume={resume}
          canAct
          onRetry={vi.fn()}
          onReuse={vi.fn()}
        />,
      );
      const step = screen.getByTestId("failure-next-step");
      expect(step.firstElementChild?.textContent).toBe(resume.reason);
      if (control !== null) {
        const button = screen.getByTestId(control);
        expect(
          step.firstElementChild!.compareDocumentPosition(button) &
            Node.DOCUMENT_POSITION_FOLLOWING,
        ).toBeTruthy();
      }
      view.unmount();
    };

    reasonThenControl(offer(), "retry-turn");
    reasonThenControl(
      offer({ state: "blocked", reason: "One step never reported back.", unreconciled_tool_call_id: "tc" }),
      "reuse-turn",
    );
    reasonThenControl(offer({ state: "unavailable", reason: "Bisby is turned off." }), null);
  });

  it("says it is working rather than going quiet while the request is in flight", () => {
    render(
      <FailureCard message={failureMessage()} agentName="Bisby" resume={offer()} canAct retrying />,
    );

    const button = screen.getByTestId("retry-turn") as HTMLButtonElement;
    expect(button.textContent).toContain("Trying again…");
    expect(button.disabled).toBe(true);
  });

  it("will not repeat a step nobody can account for, and says why", () => {
    const onRetry = vi.fn();
    const onReuse = vi.fn();
    render(
      <FailureCard
        message={failureMessage()}
        agentName="Bisby"
        resume={offer({
          state: "blocked",
          reason:
            "One step that was making a change in GitHub never reported back, so it may already have gone through. Trying again could repeat it. Check how it turned out, then tell Bisby what to do next.",
          unreconciled_tool_call_id: "tc-1",
        })}
        canAct
        onRetry={onRetry}
        onReuse={onReuse}
      />,
    );

    expect(screen.queryByTestId("retry-turn")).toBeNull();
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByTestId("failure-next-step").textContent).toContain(
      "may already have gone through",
    );

    // But it is not a dead end: the words go back in the composer and the
    // person decides.
    fireEvent.click(screen.getByTestId("reuse-turn"));
    expect(onReuse).toHaveBeenCalledWith("list the top level files");
  });

  it("names what is in the way instead of showing a button that cannot work", () => {
    render(
      <FailureCard
        message={failureMessage()}
        agentName="Bisby"
        resume={offer({
          state: "unavailable",
          reason: "Bisby is paused by an admin, so this can't be picked up right now.",
        })}
        canAct
      />,
    );

    expect(screen.queryByTestId("retry-turn")).toBeNull();
    expect(screen.queryByTestId("reuse-turn")).toBeNull();
    expect(screen.getByTestId("failure-next-step").textContent).toContain("paused by an admin");
  });

  it("gives a viewer the explanation without the controls", () => {
    render(
      <FailureCard message={failureMessage()} agentName="Bisby" resume={offer()} canAct={false} />,
    );

    expect(screen.queryByTestId("retry-turn")).toBeNull();
    expect(screen.getByTestId("failure-card").textContent).toContain("A step was cut short");
    // And not the sentence about picking it back up: it is not true for them.
    expect(screen.queryByTestId("failure-next-step")).toBeNull();
  });

  it("still tells a viewer why a failure cannot simply be repeated", () => {
    render(
      <FailureCard
        message={failureMessage()}
        agentName="Bisby"
        resume={offer({ state: "blocked", reason: "One step never reported back." })}
        canAct={false}
      />,
    );

    expect(screen.getByTestId("failure-next-step").textContent).toContain("never reported back");
    expect(screen.queryByTestId("reuse-turn")).toBeNull();
  });

  it("only offers the turn the offer is about", () => {
    render(
      <FailureCard
        message={failureMessage({ id: "m-old", task_id: "t-old" })}
        agentName="Bisby"
        resume={offer({ task_id: "t-newer" })}
        canAct
        onRetry={vi.fn()}
      />,
    );

    // A live button on a failure the conversation has already moved past
    // would re-run a question two exchanges old.
    expect(screen.queryByTestId("failure-next-step")).toBeNull();
  });

  it("keeps the specific cure and the retry on the same card", () => {
    render(
      <FailureCard
        message={failureMessage({
          content_json: { text: "Run failed: no credit", error_code: "insufficient_funds" },
          failure: notice({
            code: "insufficient_funds",
            summary: "The model provider declined the request over billing.",
            detail: "openai: 429 insufficient_quota",
            reference: "",
          }),
        })}
        agentName="Bisby"
        resume={offer()}
        canAct
        onRetry={vi.fn()}
      />,
    );

    const card = screen.getByTestId("failure-card");
    expect(card.dataset.code).toBe("insufficient_funds");
    expect(card.textContent).toContain("Out of credit");
    expect(card.textContent).toContain("openai: 429 insufficient_quota");
    expect(screen.getByRole("link", { name: "Open Models" }).getAttribute("href")).toBe("/models");
    // Topping up and then hunting for the retry is the flow this avoids.
    expect(screen.getByTestId("retry-turn")).toBeTruthy();
    expect(screen.queryByTestId("failure-reference")).toBeNull();
  });
});

describe("Transcript", () => {
  it("routes a failed run to the failure card rather than a system chip", () => {
    const onRetry = vi.fn();
    render(
      <Transcript
        items={mergeTimeline([failureMessage()], [])}
        agentName="Bisby"
        userName="Ada"
        resume={offer()}
        canRetry
        onRetry={onRetry}
      />,
    );

    expect(screen.queryByTestId("system-card")).toBeNull();
    expect(screen.getByTestId("failure-card")).toBeTruthy();
    fireEvent.click(screen.getByTestId("retry-turn"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
