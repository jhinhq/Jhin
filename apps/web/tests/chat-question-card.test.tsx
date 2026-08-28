/**
 * The choice box an agent puts in the chat (ask-user contract §5.3): options,
 * the free-text row, the states it can be in, and the promise that it cannot
 * be answered twice.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QuestionCard } from "@/components/chat/question-card";
import { Transcript } from "@/components/chat/transcript";
import { mergeTimeline } from "@/lib/chat";
import { ApiError } from "@/lib/api";
import type { AnswerQuestionOut, ConversationMessage, UserQuestionStatus } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({}));

afterEach(cleanup);

const QUESTION =
  "Is this deployment schedule only for the Engineering team, or does the whole company deploy on Mondays at 9am PST?";

function questionMessage(content: Record<string, unknown> = {}): ConversationMessage {
  return {
    id: "m-question",
    task_id: "t1",
    run_id: "r1",
    sender_type: "agent",
    sender_id: "a1",
    message_type: "question",
    created_at: "2026-08-27T10:04:11.220Z",
    conversation_id: "c1",
    sender_name: "Ada",
    agent_id: "a1",
    content_json: {
      kind: "user_question",
      question_id: "q-1",
      question: QUESTION,
      context: "You told me we deploy on Mondays at 9am PST.",
      question_kind: "memory_scope",
      options: [
        { value: "team", label: "Only the Engineering team", detail: "Saved for your team" },
        { value: "workspace", label: "Company wide", detail: "Saved for everyone" },
      ],
      allow_other: true,
      other_label: "Something else",
      other_placeholder: "Tell me in your own words…",
      status: "pending" as UserQuestionStatus,
      expires_at: "2026-08-27T10:34:11.220Z",
      asked_by_agent_name: "Ada",
      delivered: "observation",
      ...content,
    },
  };
}

function answerResult(resumed: boolean): AnswerQuestionOut {
  return {
    resumed,
    question: {
      id: "q-1",
      workspace_id: "w1",
      conversation_id: "c1",
      task_id: "t1",
      message_id: "m-question",
      agent_id: "a1",
      agent_name: "Ada",
      kind: "memory_scope",
      question: QUESTION,
      context: "",
      options: [],
      allow_other: true,
      status: "answered",
      asked_at: "2026-08-27T10:04:11.220Z",
      expires_at: "2026-08-27T10:34:11.220Z",
      answered_at: "2026-08-27T10:06:02.100Z",
      answered_by_user_id: "u1",
      answered_by_name: "Varand",
      answer_kind: "option",
      answer_option_value: "team",
      answer_text: "Only the Engineering team",
      granted_scope: "team",
      grant_denied_reason: "",
    },
  };
}

function renderCard(
  message: ConversationMessage,
  props: Partial<React.ComponentProps<typeof QuestionCard>> = {},
) {
  return render(
    <QuestionCard message={message} userName="Varand" canAnswer onAnswer={vi.fn()} {...props} />,
  );
}

describe("QuestionCard — pending", () => {
  it("shows the question, its context, every option, and the free-text row", () => {
    renderCard(questionMessage());

    expect(screen.getByTestId("question-card").getAttribute("data-state")).toBe("pending");
    expect(screen.getByText(QUESTION)).toBeTruthy();
    expect(screen.getByText("You told me we deploy on Mondays at 9am PST.")).toBeTruthy();
    expect(screen.getByText("Ada needs an answer")).toBeTruthy();

    const team = screen.getByTestId("question-option-team");
    expect(team.textContent).toContain("Only the Engineering team");
    expect(team.textContent).toContain("Saved for your team");
    expect(screen.getByTestId("question-option-workspace")).toBeTruthy();
    expect(screen.getByTestId("question-other").textContent).toContain("Something else");
  });

  it("groups the options as a radiogroup labelled by the question", () => {
    renderCard(questionMessage());
    const group = screen.getByRole("radiogroup");
    expect(group.getAttribute("aria-labelledby")).toBe(screen.getByText(QUESTION).id);
    expect(screen.getAllByRole("radio")).toHaveLength(2);
  });

  it("moves focus with the arrow keys without answering", () => {
    const onAnswer = vi.fn();
    renderCard(questionMessage(), { onAnswer });
    const team = screen.getByTestId("question-option-team");
    const workspace = screen.getByTestId("question-option-workspace");

    team.focus();
    fireEvent.keyDown(team, { key: "ArrowDown" });
    expect(document.activeElement).toBe(workspace);
    // Roving tabindex: only the focused option is in the tab order.
    expect(workspace.getAttribute("tabindex")).toBe("0");
    expect(team.getAttribute("tabindex")).toBe("-1");

    fireEvent.keyDown(workspace, { key: "ArrowUp" });
    expect(document.activeElement).toBe(team);
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("posts the option value when one is clicked, then collapses to the answer", async () => {
    const onAnswer = vi.fn().mockResolvedValue(answerResult(true));
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-option-team"));

    expect(onAnswer).toHaveBeenCalledWith("q-1", { option_value: "team" });
    await waitFor(() =>
      expect(screen.getByTestId("question-card").getAttribute("data-state")).toBe("answered"),
    );
    expect(screen.getByTestId("question-answer").textContent).toContain(
      "You answered: Only the Engineering team",
    );
    expect(screen.queryByTestId("question-option-team")).toBeNull();
  });

  it("posts free text from the other row and never as an option", async () => {
    const onAnswer = vi.fn().mockResolvedValue(answerResult(true));
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-other"));
    const input = screen.getByTestId("question-other-input");
    fireEvent.change(input, { target: { value: "  Only the platform pod  " } });
    fireEvent.click(screen.getByTestId("question-other-send"));

    expect(onAnswer).toHaveBeenCalledWith("q-1", { other_text: "Only the platform pod" });
    await waitFor(() =>
      expect(screen.getByTestId("question-answer").textContent).toContain("Only the platform pod"),
    );
  });

  it("sends free text on Cmd/Ctrl+Enter", async () => {
    const onAnswer = vi.fn().mockResolvedValue(answerResult(true));
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-other"));
    const input = screen.getByTestId("question-other-input");
    fireEvent.change(input, { target: { value: "The platform pod" } });
    fireEvent.keyDown(input, { key: "Enter", metaKey: true });

    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith("q-1", { other_text: "The platform pod" }),
    );
  });

  it("refuses to send an empty free-text answer", () => {
    const onAnswer = vi.fn();
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-other"));
    fireEvent.change(screen.getByTestId("question-other-input"), { target: { value: "   " } });
    const send = screen.getByTestId("question-other-send");
    expect((send as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(send);
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("cannot be answered twice: the second click never reaches the API", async () => {
    // A request that never settles is the window a double-click lands in.
    const onAnswer = vi.fn().mockReturnValue(new Promise<AnswerQuestionOut>(() => {}));
    renderCard(questionMessage(), { onAnswer });

    const team = screen.getByTestId("question-option-team");
    fireEvent.click(team);
    await waitFor(() => expect((team as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(team);
    fireEvent.click(screen.getByTestId("question-option-workspace"));

    expect(onAnswer).toHaveBeenCalledTimes(1);
    // The one being sent is the one that reads as chosen.
    expect(team.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("question-option-workspace").getAttribute("aria-checked")).toBe("false");
  });

  it("puts the buttons back and says what went wrong when the answer is refused", async () => {
    const onAnswer = vi.fn().mockRejectedValue(new ApiError(409, "Varand already answered this."));
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-option-team"));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("already answered"));
    expect(screen.getByTestId("question-card").getAttribute("data-state")).toBe("pending");
    expect((screen.getByTestId("question-option-team") as HTMLButtonElement).disabled).toBe(false);
  });

  it("tells the person to use the composer when the run had stopped waiting", async () => {
    const onAnswer = vi.fn().mockResolvedValue(answerResult(false));
    renderCard(questionMessage(), { onAnswer });

    fireEvent.click(screen.getByTestId("question-option-team"));

    await waitFor(() =>
      expect(screen.getByTestId("question-not-resumed").textContent).toContain(
        "Ada had already stopped waiting",
      ),
    );
  });

  it("disables every control for a viewer and says why", () => {
    renderCard(questionMessage(), { canAnswer: false });

    expect((screen.getByTestId("question-option-team") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("question-option-workspace") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("question-other") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("question-option-team").getAttribute("title")).toBe(
      "Viewers can read chats but can't answer.",
    );
    expect(screen.getAllByText("Viewers can read chats but can't answer.").length).toBeGreaterThan(0);
  });

  it("hides the free-text row when the question does not allow one", () => {
    renderCard(questionMessage({ allow_other: false }));
    expect(screen.queryByTestId("question-other")).toBeNull();
    expect(screen.getByTestId("question-option-team")).toBeTruthy();
  });
});

describe("QuestionCard — settled states", () => {
  it("renders a picked answer with no controls", () => {
    renderCard(
      questionMessage({
        status: "answered",
        answer_kind: "option",
        answer_option_value: "team",
        answer: "Only the Engineering team",
        answered_by_name: "Varand",
        answered_at: "2026-08-27T10:06:02.100Z",
      }),
    );

    expect(screen.getByTestId("question-answer").textContent).toContain(
      "You answered: Only the Engineering team",
    );
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByTestId("question-other")).toBeNull();
  });

  it("names the other person when somebody else answered", () => {
    renderCard(
      questionMessage({
        status: "answered",
        answer_kind: "other",
        answer: "Only the platform pod",
        answered_by_name: "Sam",
      }),
    );
    expect(screen.getByTestId("question-answer").textContent).toContain(
      "Sam answered: Only the platform pod",
    );
  });

  it("says the agent stopped waiting when the question expired", () => {
    renderCard(questionMessage({ status: "expired" }));
    expect(screen.getByTestId("question-card").getAttribute("data-state")).toBe("expired");
    expect(screen.getByTestId("question-closed").textContent).toContain(
      "Ada stopped waiting. Answer in the message box below",
    );
    // The question itself stays readable.
    expect(screen.getByText(QUESTION)).toBeTruthy();
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("says the agent stopped before an answer when the run was cancelled", () => {
    renderCard(questionMessage({ status: "cancelled" }));
    expect(screen.getByTestId("question-closed").textContent).toBe(
      "Ada stopped before you answered.",
    );
  });

  it("renders a question whose options arrived malformed rather than throwing", () => {
    renderCard(questionMessage({ options: [{ label: "no value" }, "junk", null] }));
    expect(screen.getByText(QUESTION)).toBeTruthy();
    expect(screen.queryByRole("radiogroup")).toBeNull();
    // The escape hatch is still there, which is the whole point of it.
    expect(screen.getByTestId("question-other")).toBeTruthy();
  });
});

describe("Transcript integration", () => {
  const timeline = () => mergeTimeline([questionMessage()], []);

  it("renders the question as its own card, not a work card", () => {
    render(
      <Transcript
        items={timeline()}
        agentName="Ada"
        userName="Varand"
        canAnswer
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByTestId("question-card")).toBeTruthy();
    expect(screen.queryByTestId("work-card")).toBeNull();
  });

  it("hands the card's answer through to onAnswer", () => {
    const onAnswer = vi.fn();
    render(
      <Transcript
        items={timeline()}
        agentName="Ada"
        userName="Varand"
        canAnswer
        onAnswer={onAnswer}
      />,
    );
    fireEvent.click(screen.getByTestId("question-option-workspace"));
    expect(onAnswer).toHaveBeenCalledWith("q-1", { option_value: "workspace" });
  });

  it("says the chat is waiting on the person, not that the agent is working", () => {
    render(
      <Transcript
        items={timeline()}
        agentName="Ada"
        userName="Varand"
        liveStatus={{ label: "Needs your answer", tone: "warn", kind: "question" }}
      />,
    );
    const indicator = screen.getByTestId("working-indicator");
    expect(indicator.textContent).toBe("Waiting for your answer — see the question above.");
    expect(indicator.textContent).not.toContain("working");
  });
});
