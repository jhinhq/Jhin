"use client";

/**
 * The choice box an agent puts in the chat when it needs the person to decide
 * something it should not guess at — most often which memory a fact belongs
 * to ("only Engineering, or the whole company?").
 *
 * The agent's run is parked on this card, so it is deliberately the loudest
 * thing in the transcript while it is open and the quietest once it is
 * answered. The options are the answer; the free-text row is the escape
 * hatch, styled to read that way — secondary, but never hidden, because a
 * question whose real answer is not on the list is exactly the question worth
 * asking.
 *
 * Renders the ask-user contract §5.1 message shape; posts to §2.1.
 */

import { Check, Pencil } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { Timestamp } from "@/components/chat/timestamp";
import { Button, focusRing } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { readUserQuestion } from "@/lib/chat";
import { avatarProps } from "@/lib/media";
import type {
  AgentAvatar,
  AnswerQuestionIn,
  AnswerQuestionOut,
  ConversationMessage,
} from "@/lib/types";

/** Matches the API's `other_text` ceiling (ask-user §6), which is itself the
 * longest text memory stores verbatim — so nothing an answer says is ever
 * truncated between this box and the memory it becomes. */
const MAX_OTHER_CHARS = 2000;

const VIEWER_NOTE = "Viewers can read chats but can't answer.";

export type AnswerQuestion = (
  questionId: string,
  body: AnswerQuestionIn,
) => void | Promise<AnswerQuestionOut | void>;

/** What was picked, held locally from the click until the server echoes the
 * mutated message back. */
interface Choice {
  label: string;
  optionValue: string;
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  return "Couldn't send your answer. Check your connection and try again.";
}

export function QuestionCard({
  message,
  userName,
  avatar,
  canAnswer = false,
  answering = false,
  onAnswer,
}: {
  message: ConversationMessage;
  /** The signed-in person, so an answer they gave reads "You answered". */
  userName: string;
  avatar?: AgentAvatar | null;
  canAnswer?: boolean;
  /** True while an answer on this thread is in flight. */
  answering?: boolean;
  onAnswer?: AnswerQuestion;
}) {
  const headingId = useId();
  const otherId = useId();
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const otherRef = useRef<HTMLTextAreaElement | null>(null);
  const [focusIndex, setFocusIndex] = useState(0);
  const [otherOpen, setOtherOpen] = useState(false);
  const [otherText, setOtherText] = useState("");
  const [choice, setChoice] = useState<Choice | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stoppedWaiting, setStoppedWaiting] = useState(false);

  // The row only exists once the person opens it, so focus has to wait for
  // the commit rather than happen inside the click handler.
  useEffect(() => {
    if (otherOpen) otherRef.current?.focus();
  }, [otherOpen]);

  const question = readUserQuestion(message);
  if (question === null) return null;

  const agentName = question.asked_by_agent_name || message.sender_name || "Your agent";
  const serverAnswered = question.status === "answered";
  // Optimistic: the moment the request comes back we show the answer, without
  // waiting for the poller to bring the mutated message round.
  const answered = serverAnswered || (choice !== null && !submitting);
  const closed = !answered && (question.status === "expired" || question.status === "cancelled");
  // One gate for every control. A viewer, an answer already in flight, and a
  // question the server has closed all mean the same thing here, and going
  // through one flag is what makes the card impossible to answer twice.
  const locked = !canAnswer || submitting || answering || answered || closed;

  const send = async (picked: Choice, body: AnswerQuestionIn) => {
    if (locked) return;
    setError(null);
    setStoppedWaiting(false);
    setChoice(picked);
    setSubmitting(true);
    try {
      const result = await onAnswer?.(question.question_id, body);
      if (result && result.resumed === false) setStoppedWaiting(true);
    } catch (failure) {
      // Put the controls back rather than leave a card claiming an answer the
      // agent never received.
      setChoice(null);
      setError(describeError(failure));
    } finally {
      setSubmitting(false);
    }
  };

  const moveFocus = (from: number, delta: number) => {
    const count = question.options.length;
    if (count === 0) return;
    const next = (from + delta + count) % count;
    setFocusIndex(next);
    optionRefs.current[next]?.focus();
  };

  // Arrow keys move focus without choosing. A plain radio group selects as it
  // moves; here selecting *sends*, and an answer is not something to undo, so
  // the person still presses Enter or Space on the one they mean.
  const onOptionKeyDown = (index: number) => (event: React.KeyboardEvent) => {
    const step: Record<string, number> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    if (event.key in step) {
      event.preventDefault();
      moveFocus(index, step[event.key]);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveFocus(-1, 1);
    } else if (event.key === "End") {
      event.preventDefault();
      moveFocus(0, -1);
    }
  };

  const sendOther = () => {
    const text = otherText.trim();
    if (!text) return;
    void send({ label: text, optionValue: "" }, { other_text: text });
  };

  const shell = (state: string, body: React.ReactNode) => (
    <div data-testid="question-card" data-state={state} className="flex items-start gap-2.5">
      <Avatar name={agentName} size="sm" {...avatarProps(avatar)} />
      <div
        aria-busy={submitting || undefined}
        className={`min-w-0 max-w-[min(85%,40rem)] flex-1 rounded-2xl border px-4 py-3 ${
          state === "pending"
            ? "border-accent/30 bg-raised shadow-[var(--card-shadow)]"
            : "border-line bg-raised"
        }`}
      >
        {body}
      </div>
    </div>
  );

  if (answered) {
    const answerText = serverAnswered ? question.answer : (choice?.label ?? "");
    const by = serverAnswered ? (question.answered_by_name ?? "") : userName;
    const mine = by === "" || by === userName;
    return shell(
      "answered",
      <>
        <div className="flex items-start justify-between gap-2">
          <p className="min-w-0 text-[13px] leading-relaxed text-dim">{question.question}</p>
          <Timestamp iso={message.created_at} className="shrink-0" />
        </div>
        <p data-testid="question-answer" className="mt-1.5 flex items-start gap-1.5 text-sm text-ink">
          <Check size={15} aria-hidden className="mt-0.5 shrink-0 text-ok" />
          <span className="min-w-0 break-words">
            <span className="text-dim">{mine ? "You answered" : `${by} answered`}: </span>
            {answerText}
          </span>
        </p>
        {stoppedWaiting ? (
          <p data-testid="question-not-resumed" className="mt-1.5 text-[13px] text-dim">
            Sent — {agentName} had already stopped waiting. Send it as a message and it will pick
            it up.
          </p>
        ) : null}
      </>,
    );
  }

  if (closed) {
    return shell(
      question.status,
      <>
        <div className="flex items-start justify-between gap-2">
          <p className="min-w-0 text-[15px] leading-relaxed text-ink">{question.question}</p>
          <Timestamp iso={message.created_at} className="shrink-0" />
        </div>
        <p data-testid="question-closed" className="mt-1.5 text-[13px] text-dim">
          {question.status === "expired"
            ? `${agentName} stopped waiting. Answer in the message box below and it will pick it up.`
            : `${agentName} stopped before you answered.`}
        </p>
      </>,
    );
  }

  const viewerTitle = canAnswer ? undefined : VIEWER_NOTE;

  return shell(
    "pending",
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-ink">{agentName} needs an answer</p>
        <Timestamp iso={message.created_at} />
      </div>
      <p id={headingId} className="mt-1 text-[15px] leading-relaxed text-ink">
        {question.question}
      </p>
      {question.context ? (
        <p className="mt-1 text-[13px] leading-relaxed text-dim">{question.context}</p>
      ) : null}

      {question.options.length > 0 ? (
        <ul role="radiogroup" aria-labelledby={headingId} className="mt-3 space-y-1.5">
          {question.options.map((option, index) => {
            const picked = choice !== null && choice.optionValue === option.value;
            return (
              <li key={option.value} role="none">
                <button
                  type="button"
                  role="radio"
                  aria-checked={picked}
                  ref={(node) => {
                    optionRefs.current[index] = node;
                  }}
                  data-testid={`question-option-${option.value}`}
                  tabIndex={index === focusIndex ? 0 : -1}
                  disabled={locked}
                  title={viewerTitle}
                  onKeyDown={onOptionKeyDown(index)}
                  onFocus={() => setFocusIndex(index)}
                  onClick={() =>
                    void send({ label: option.label, optionValue: option.value }, {
                      option_value: option.value,
                    })
                  }
                  className={`flex min-h-[44px] w-full items-start gap-2.5 rounded-xl border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed ${focusRing} ${
                    picked
                      ? "border-accent bg-accent-soft"
                      : "border-line bg-surface hover:border-line-strong hover:bg-hover disabled:opacity-60 disabled:hover:border-line disabled:hover:bg-surface"
                  }`}
                >
                  <span
                    aria-hidden
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                      picked ? "border-accent bg-accent text-bg" : "border-line-strong"
                    }`}
                  >
                    {picked ? <Check size={11} strokeWidth={3} /> : null}
                  </span>
                  <span className="min-w-0">
                    <span className="block break-words text-sm text-ink">{option.label}</span>
                    {option.detail ? (
                      <span className="mt-0.5 block break-words text-xs text-faint">
                        {option.detail}
                      </span>
                    ) : null}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}

      {question.allow_other && !otherOpen ? (
        <button
          type="button"
          data-testid="question-other"
          disabled={locked}
          title={viewerTitle}
          onClick={() => setOtherOpen(true)}
          className={`mt-1.5 flex min-h-[40px] w-full items-center gap-2 rounded-xl border border-dashed border-line px-3 py-2 text-left text-[13px] text-dim transition-colors hover:border-line-strong hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`}
        >
          <Pencil size={13} aria-hidden className="shrink-0" />
          <span className="min-w-0 truncate">{question.other_label}</span>
        </button>
      ) : null}

      {question.allow_other && otherOpen ? (
        <div className="mt-1.5 rounded-xl border border-line-strong bg-surface p-2.5">
          <label htmlFor={otherId} className="text-xs font-medium text-dim">
            {question.other_label}
          </label>
          <textarea
            id={otherId}
            ref={otherRef}
            data-testid="question-other-input"
            rows={2}
            maxLength={MAX_OTHER_CHARS}
            value={otherText}
            disabled={locked}
            placeholder={question.other_placeholder}
            onChange={(event) => setOtherText(event.target.value)}
            onKeyDown={(event) => {
              // Cmd/Ctrl+Enter sends; a bare Enter stays a newline, because
              // this is the row for the answer that needs a sentence.
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                sendOther();
              }
            }}
            className="mt-1 w-full resize-y rounded-xl border border-line bg-surface px-3 py-2 text-base text-ink outline-none transition-colors placeholder:text-faint focus:border-accent focus:ring-2 focus:ring-accent/25 disabled:opacity-50 md:text-sm"
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={submitting}
              onClick={() => setOtherOpen(false)}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="primary"
              data-testid="question-other-send"
              disabled={locked || otherText.trim() === ""}
              onClick={sendOther}
            >
              {submitting ? "Sending…" : "Send"}
            </Button>
          </div>
        </div>
      ) : null}

      {!canAnswer ? <p className="mt-2 text-[13px] text-dim">{VIEWER_NOTE}</p> : null}
      {error ? (
        <p role="alert" className="mt-2 text-[13px] text-danger">
          {error}
        </p>
      ) : null}
    </>,
  );
}
