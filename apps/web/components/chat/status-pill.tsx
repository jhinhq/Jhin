"use client";

/** Small live status pill (text + color, never color alone). */

import {
  ElapsedWorkingTime,
  StalledWorkingTime,
  WORKING_TIME_BANKED_TITLE,
  WORKING_TIME_UNAVAILABLE_TITLE,
} from "@/components/chat/working-time";
import { statusLabelFor, type LiveStatus, type LiveStatusSource } from "@/lib/chat";

const TONES: Record<LiveStatus["tone"], string> = {
  accent: "bg-accent-soft text-accent-strong border-accent/30",
  neutral: "bg-hover text-dim border-line-strong",
  warn: "bg-warn/10 text-warn border-warn/30",
};

export function LiveStatusPill({
  conversation,
  className = "",
}: {
  conversation: LiveStatusSource;
  className?: string;
}) {
  const status = statusLabelFor(conversation);
  if (!status) return null;
  // Working, and the API measured no instant to count from (`since === null`,
  // never merely absent — an API too old to send the field said nothing, and
  // nothing is what this pill then says back). Not a wait: a wait has its own
  // label.
  //
  // Several different turns land here — one parked on an approval, question
  // or review nobody closed, one whose run has already finished, one whose
  // start was never stamped — and they arrive as the same null, so nothing
  // below picks one of them to name. The pill has no room to explain either
  // way; hover and a screen reader get the words, and the transcript's
  // indicator says them on the surface. See `WORKING_TIME_UNAVAILABLE_TITLE`.
  const stalled = status.kind === "working" && status.since === null;
  const banked = stalled ? (status.worked ?? 0) : 0;
  // The number carries its own state word wherever the label is the agent's
  // step rather than the word "Working" — see `ElapsedWorkingTime`.
  const withState = status.specific === true;
  return (
    <span
      data-testid="live-status"
      data-kind={status.kind}
      title={
        stalled
          ? banked > 0
            ? WORKING_TIME_BANKED_TITLE
            : WORKING_TIME_UNAVAILABLE_TITLE
          : undefined
      }
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONES[status.tone]} ${className}`}
    >
      {status.kind === "working" ? (
        <span
          aria-hidden
          className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
        />
      ) : null}
      {/* "Working…" is the one label that never clips, because it is the one
       * sharing its pill with a live number. At 320px with a six-character
       * timer beside it, `truncate` here turned it into "Workin…" — which
       * reads as a bug in the product rather than as a label that ran out of
       * room, and it is eight characters that always fit.
       *
       * Everything else does clip. A long wait ("Waiting for a free slot") is
       * a whole phrase and truncating it reads as truncation; an activity
       * sentence ("Making a change in GitHub") is written to be shortened and
       * still says what it is on hover. */}
      {status.kind === "working" && !status.specific ? (
        <span className="shrink-0 whitespace-nowrap">{status.label}</span>
      ) : (
        <span className="truncate" title={status.specific ? status.label : undefined}>
          {status.label}
        </span>
      )}
      {/* Last, and never truncated: the label may run out of room, the few
       * characters saying how long it has been going should not. `since` is
       * set only while the agent is genuinely working (see `statusLabelFor`),
       * so a wait shows no clock — and neither does an API too old to send
       * the working clock, which is better than counting the wrong thing.
       *
       * Three branches for three states, and the last one is the point: a
       * `since` that is absent rather than null is an API that never measured
       * anything, and it gets no number, no note and no tooltip. */}
      {status.since ? (
        <ElapsedWorkingTime since={status.since} worked={status.worked ?? 0} withState={withState} />
      ) : stalled ? (
        <StalledWorkingTime worked={banked} withState={withState} />
      ) : null}
    </span>
  );
}
