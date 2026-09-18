"use client";

/**
 * What a person sees when a turn didn't finish, and what they can do about it.
 *
 * A failure used to arrive here as the run's own debugging note — "Run
 * failed: tool call a34dd1dc-… execution outcome is unknown; manual
 * reconciliation is required" — which is a sentence written for whoever owns
 * the incident, shown to whoever was mid-conversation. The API now sends a
 * `FailureNotice` beside the raw text: one sentence in the product's voice,
 * the failure's own words underneath where they add something, and the
 * identifier kept for support but never leading.
 *
 * The card also carries the way out, because a failure with no next step is
 * how a chat gets stuck:
 *
 * - `ready` — one press sends the same message again. Nothing to retype.
 * - `blocked` — a step from that turn was never accounted for, so it may
 *   already have gone through. This card will not quietly repeat it. It says
 *   why, and offers the words back in the composer so the person decides.
 * - `unavailable` — the turn is fine to send; the agent or the chat is not
 *   ready for it. The reason names what to change.
 *
 * All three get the same shape — a rule, the sentence, then whatever control
 * that state has — so the card reads as "here is what happened / here is what
 * happens next" rather than as three paragraphs of prose that happen to end
 * in a button.
 *
 * Some codes also have a specific cure (out of credit, an unusable model
 * setting), and those keep their link to Models — a person who tops up wants
 * the retry button right there afterwards, so the link and the control sit on
 * the same card rather than replacing each other.
 */

import { AlertTriangle, RotateCcw } from "lucide-react";
import Link from "next/link";
import { Timestamp } from "@/components/chat/timestamp";
import { Button } from "@/components/ui";
import { readFailure } from "@/lib/chat";
import { INSUFFICIENT_FUNDS_CODE, MODEL_INCOMPATIBLE_REQUEST_CODE } from "@/lib/models";
import type { ConversationMessage, ConversationResume } from "@/lib/types";

/** Failures whose cure is a specific screen rather than trying again. */
const FIXES: Record<string, { heading: string; href: string; label: string; after: string }> = {
  [INSUFFICIENT_FUNDS_CODE]: {
    heading: "Out of credit",
    href: "/models",
    label: "Open Models",
    after: " to check the balance.",
  },
  [MODEL_INCOMPATIBLE_REQUEST_CODE]: {
    heading: "Model setting needs a change",
    href: "/models",
    label: "Open Models",
    after: " to edit the model profile.",
  },
};

export function FailureCard({
  message,
  agentName,
  resume = null,
  canAct = false,
  retrying = false,
  onRetry,
  onReuse,
}: {
  message: ConversationMessage;
  agentName: string;
  /** The offer for this conversation's newest failure, when there is one. */
  resume?: ConversationResume | null;
  /** Member or above: a viewer reads the failure but cannot act on it. */
  canAct?: boolean;
  retrying?: boolean;
  onRetry?: () => void;
  /** Put the words back in the composer, focused, without sending. */
  onReuse?: (text: string) => void;
}) {
  const notice = readFailure(message);
  if (notice === null) return null;

  const fix = FIXES[notice.code];
  const heading = fix?.heading ?? `${agentName} couldn't finish that`;
  // The offer describes one turn. Showing it on an older failure further up
  // the thread would put a live button on a question the person has already
  // moved past.
  const matched = resume && resume.task_id === message.task_id ? resume : null;
  // "It can be picked up" is only worth saying to somebody who could pick it
  // up. A viewer reads the failure and stops there; the other two states name
  // something true for everyone, so they stay.
  const offer = matched?.state === "ready" && !canAct ? null : matched;
  const instruction = offer?.instruction.trim() ?? "";
  const canReuse = canAct && Boolean(onReuse) && instruction.length > 0;

  return (
    <div data-testid="failure-card" data-code={notice.code} className="flex justify-center">
      <div className="max-w-[min(90%,36rem)] rounded-2xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-ink">
        {/* `items-start`, not centre: a long agent name wraps to two or three
         * lines on a phone, and a centred icon then floats beside the middle
         * of the sentence instead of marking its beginning. */}
        <p className="flex items-start gap-2 font-medium text-danger">
          <AlertTriangle size={15} aria-hidden className="mt-[3px] shrink-0" />
          <span className="min-w-0 break-words">{heading}</span>
        </p>
        <p className="mt-1 break-words text-dim">{notice.summary}</p>
        {notice.detail ? (
          // Set off as a quotation, because that is what it is: a provider's
          // sentence or a command's output, not Jhin talking. Run flush with
          // the summary it can be four lines long and, at the same size and
          // colour, simply outweighs it — the card ends up led by somebody
          // else's stack trace.
          <p className="mt-2 break-words border-l-2 border-danger/25 pl-2.5 text-[13px] text-dim">
            {notice.detail}
          </p>
        ) : null}
        {fix ? (
          <p className="mt-2 text-xs">
            <Link
              href={fix.href}
              className="font-medium text-accent-strong underline-offset-2 hover:underline"
            >
              {fix.label}
            </Link>
            <span className="text-faint">{fix.after}</span>
          </p>
        ) : null}

        {offer ? (
          // One shape for all three states: a rule, then what happens next,
          // then the control if there is one. Without the rule the offer is a
          // third paragraph of the same size in the same colour, and the
          // blocked card in particular reads as one wall of prose with a
          // button at the bottom — the reader has to parse three sentences
          // before anything tells them a decision is being asked of them.
          <div
            data-testid="failure-next-step"
            data-state={offer.state}
            className="mt-3 border-t border-danger/25 pt-3"
          >
            <p className="break-words text-[13px] text-dim">{offer.reason}</p>
            {offer.state === "ready" && canAct ? (
              <Button
                type="button"
                size="sm"
                variant="primary"
                className="mt-2"
                data-testid="retry-turn"
                disabled={retrying}
                onClick={() => onRetry?.()}
              >
                <RotateCcw size={14} aria-hidden />
                {retrying ? "Trying again…" : "Try again"}
              </Button>
            ) : null}
            {offer.state === "blocked" && canReuse ? (
              <Button
                type="button"
                size="sm"
                className="mt-2"
                data-testid="reuse-turn"
                onClick={() => onReuse?.(instruction)}
              >
                Edit and send again
              </Button>
            ) : null}
          </div>
        ) : null}

        <p className="mt-3 flex flex-wrap items-center gap-x-2 text-[11px] text-faint">
          <Timestamp iso={message.created_at} />
          {notice.reference ? (
            // Quiet, last, and selectable: a person only reads this when
            // somebody in support asks them to. `break-all` because a bare
            // identifier has nowhere to wrap on a phone.
            <span data-testid="failure-reference" className="break-all">
              Reference {notice.reference}
            </span>
          ) : null}
        </p>
      </div>
    </div>
  );
}
