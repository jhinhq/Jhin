"use client";

/**
 * How long the agent has been working on this turn — the number itself, the
 * one clock that drives every copy of it, and the words that say what it
 * measures.
 *
 * Three surfaces show it and they are not equal. The transcript's working
 * indicator is where the operator asked for it and where the reader is
 * looking, so it gets the roomy treatment: its own line under the bubble,
 * with the word "Working" attached to the number. The header pill and the
 * conversation rail are summaries measured in characters — the pill survives
 * scrolling back through a long transcript, and the rail is the only place
 * two chats can be compared ("which of these has been going twenty minutes")
 * — so they get the number alone wherever their label is already the word
 * "Working…", which is every row on the rail (the chat list does not pay for
 * an activity sentence per row) and the header whenever the API has no step
 * to name.
 *
 * Where the header's label *is* a step sentence, the number takes the state
 * word with it. A bare number against "Making a change in GitHub" reads as
 * that step's duration, and a hover title is not an answer for a reader who
 * cannot hover. The full explanation still lives on hover and in the
 * accessible name, because neither surface has room to write it out.
 */

import { useSyncExternalStore } from "react";
import { coarseElapsedLabel, elapsedLabel, elapsedSeconds } from "@/lib/chat";

/* ------------------------------------------------------------------ */
/* One clock for the page                                               */
/* ------------------------------------------------------------------ */

/**
 * A once-a-second clock, shared by every elapsed number on the page.
 *
 * The rail can show a pill per conversation and the open thread shows two
 * more, and a timer each would be a timer each. One interval serves all of
 * them and stops itself when the last one unmounts. It is read through
 * `useSyncExternalStore` because that is what the browser's clock is — a
 * mutable source outside React — and reading it during render is neither pure
 * nor allowed.
 *
 * The snapshot is rounded down to the second so React sees the same value for
 * every read within one tick.
 */
const listeners = new Set<() => void>();
let timer: number | null = null;

function subscribeToClock(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    timer = window.setInterval(() => {
      for (const each of listeners) each();
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}

function clockSnapshot(): number {
  const now = Date.now();
  return now - (now % 1000);
}

/** Nothing ticks on the server; the first client render supplies the number. */
function serverSnapshot(): number {
  return 0;
}

/* ------------------------------------------------------------------ */
/* What the number means                                                */
/* ------------------------------------------------------------------ */

/**
 * On hover, for the reading the number invites and does not deserve.
 *
 * Beside a one-step sentence ("Making a change in GitHub · 1h 12m") the digits
 * read as that step's duration, and they are not: they are the whole turn's
 * thinking. The second half is the other half of the surprise — this number
 * is smaller than the clock on the wall whenever the person took time to
 * answer something, and nothing else on screen explains the gap.
 */
export const WORKING_TIME_TITLE =
  "How long this reply has been working in total — every step of it added up, and not " +
  "counting any time it spent waiting on you.";

/**
 * The turn is working, and how long for cannot be measured.
 *
 * **It does not name one cause, because the browser cannot tell which one it
 * is.** `working_since` comes back null for several different reasons and the
 * projection sends the same null for all of them: a wait row — an approval,
 * question or review — opened and never closed; a run that has already
 * finished; a run whose own span is unusable, its start stamp missing or two
 * clocks disagreeing; and a task still marked running with no run behind it at
 * all. Asserting the first as fact was the same mistake as reading an absent
 * field as a measured one: a sentence about this workspace that is true of one
 * case and stated for every case. So the words name what is certain — there is
 * no instant to count the current stretch from — and offer the causes as the
 * possibilities they are.
 *
 * The alternative to saying nothing is counting from the run's own start,
 * which is the bug this whole feature exists to avoid. There is no excuse for
 * showing nothing *and* saying nothing, though, which is what this is for.
 */
export const WORKING_TIME_UNAVAILABLE_TITLE =
  "How long this reply has been working can't be measured: nothing here has a point in time " +
  "to count from. A pause for an approval, question or review that nobody closed does that, " +
  "and so does a turn with no start recorded.";

/**
 * The same stall, with the part the API *did* measure put back.
 *
 * A turn whose clock stopped still has a real figure behind it: everything it
 * worked up to that point, banked to the second. The clock cannot resume —
 * there is no instant to count from — but throwing that figure away to print
 * "unavailable" tells a person nothing when the truth is "sixteen seconds, and
 * then it stopped". So the number is shown with what it is: a floor, not a
 * running total.
 *
 * Two things stop a clock with seconds already on it — a wait nobody closed,
 * and a run that finished while its task still reads as running — and the
 * pair arrives here as the same null, so the words carry both rather than
 * picking one. See `WORKING_TIME_UNAVAILABLE_TITLE`.
 */
export const WORKING_TIME_BANKED_TITLE =
  "How long this reply had worked when its clock last stopped — at a pause for an approval, " +
  "question or review that nobody closed, or at the end of the run. Nothing can count on from " +
  "there, so this is a floor rather than a total.";

/* ------------------------------------------------------------------ */
/* Where one phrase ends and the next begins                            */
/* ------------------------------------------------------------------ */

/**
 * The mark between a step sentence and the number, where a pill holds both.
 *
 * The pill's own `gap-1.5` is 6px, against a 3.09px space at 11px: under two
 * space-widths between two independent phrases, at one size and in one colour,
 * so "Making a change in GitHub" and "Working 1h 12m" arrive as the single
 * ungrammatical run-on "Making a change in GitHub Working 1h 12m" and only
 * come apart into two facts on a second read. The weight difference — a
 * `font-medium` label against a `font-normal` number — is doing some of the
 * work and not enough of it this small. One glyph makes the boundary explicit:
 * the same middot the chat header already sets between a title and "Archived".
 *
 * `aria-hidden`, because it is punctuation for the eye. The accessible name
 * separates the same two phrases with a comma of its own ("Making a change in
 * GitHub, working for about 1 hour 12 minutes so far"), and a screen reader
 * reciting "middle dot" would be reading out the typography.
 *
 * It appears only where the two phrases really are independent. Beside the
 * generic label the pill reads "Working… 1h 12m", which is one phrase whose
 * ellipsis already does this job, and a dot there would break a sentence in
 * half.
 */
function PhraseBreak() {
  return (
    <span aria-hidden data-testid="live-status-phrase-break" className="mr-1.5">
      ·
    </span>
  );
}

/** Total seconds of thinking: what the API banked, plus the stretch running now. */
function useWorkedSeconds(since: string, worked: number): number | null {
  const now = useSyncExternalStore(subscribeToClock, clockSnapshot, serverSnapshot);
  const seconds = now === 0 ? null : elapsedSeconds(since, now);
  return seconds === null ? null : worked + seconds;
}

/* ------------------------------------------------------------------ */
/* The number, for a pill                                               */
/* ------------------------------------------------------------------ */

/**
 * `6s`, `1m 12s`, `2h 5m` — as its own component so a pill with nothing to
 * count never subscribes to the clock at all. Most pills on a rail are idle,
 * and an idle pill should cost nothing per second.
 *
 * `worked` is the thinking the API already banked for this turn and `since` is
 * where the current stretch started; the sum is what the number means. Only
 * the second half moves, so a turn that stopped for a question resumes
 * counting where it left off instead of restarting at zero — and none of the
 * time the person spent deciding is in either half.
 *
 * `withState` puts the word "Working" in front of the digits, and it is not
 * decoration. Beside the generic label the pill reads "Working… 1h 12m" and
 * the number is already attached to the word it measures; beside a step
 * sentence it reads "Making a change in GitHub 1h 12m", where the only thing
 * saying the number is not that step's duration is a hover title — which a
 * touch reader never sees. Carrying the state word is the pill's version of
 * the separate line the transcript gives it: the number names its own
 * subject, on the surface, without being hovered.
 *
 * The state word brings a `PhraseBreak` with it, and needs to. Two phrases
 * butted together at 11px in one colour still read as one run of text —
 * "Making a change in GitHub Working 1h 12m" — however correct each half is,
 * and the mark is what makes the boundary visible without a second read.
 */
export function ElapsedWorkingTime({
  since,
  worked,
  withState = false,
}: {
  since: string;
  worked: number;
  withState?: boolean;
}) {
  const total = useWorkedSeconds(since, worked);
  if (total === null) return null;
  return (
    <span className="shrink-0 whitespace-nowrap" title={WORKING_TIME_TITLE}>
      {/* Inside the wrapper rather than beside it in the pill, so the mark
       * cannot outlive what it separates: this component renders nothing at
       * all before the browser's clock is readable, and a dot left hanging off
       * the end of a pill for that frame would be worse than the run-on. */}
      {withState ? <PhraseBreak /> : null}
      {/* `aria-hidden` on the digits, which rewrite themselves every second: a
       * screen reader reciting a new number each second would talk over the
       * conversation the pill sits beside. The sibling says the same thing
       * once a minute instead, so the one fact this feature adds is not
       * missing entirely — which is what `aria-hidden` and nothing else was.
       *
       * No `opacity-70`. It measured 2.68:1 against the pill's own background
       * in the light theme, where 4.5:1 is the floor for 11px text, and it is
       * this app's word for *disabled* — every other use in the product marks
       * a control that cannot be pressed. Dressing the one live, ticking
       * element on the pill as switched off was saying the opposite of what it
       * does. The hierarchy it was buying comes from weight instead: the label
       * is `font-medium`, this is not, and both are legible. */}
      <span aria-hidden data-testid="live-status-elapsed" className="font-normal tabular-nums">
        {withState ? `Working ${elapsedLabel(total)}` : elapsedLabel(total)}
      </span>
      <span className="sr-only">, working for {coarseElapsedLabel(total)} so far</span>
    </span>
  );
}

/**
 * The pill's version of a turn that is working with no stretch to count from.
 *
 * With a banked figure the honest thing is to show it, marked as a floor:
 * `≥16s` is true, is more use than silence, and cannot be mistaken for a
 * running total the way a static "16s" beside a pulsing dot could. The words
 * behind the symbol are on hover and in the accessible name, since the pill
 * has room for neither. It is the one thing here allowed a seventh character
 * — `elapsedLabel` is held to six for the number that ticks, and this one is
 * stopped, rare, and unreadable without the mark that says so.
 *
 * With nothing banked there is genuinely no figure — a turn that parked
 * before it worked a whole second — and the pill says so where only a screen
 * reader and a hover can hear it. Its own words have to stay the state.
 */
export function StalledWorkingTime({
  worked,
  withState = false,
}: {
  worked: number;
  withState?: boolean;
}) {
  if (worked <= 0) return <span className="sr-only">, working time unavailable</span>;
  const floor = `≥${elapsedLabel(worked)}`;
  return (
    <span className="shrink-0 whitespace-nowrap" title={WORKING_TIME_BANKED_TITLE}>
      {withState ? <PhraseBreak /> : null}
      <span aria-hidden data-testid="live-status-elapsed-banked" className="font-normal tabular-nums">
        {withState ? `Worked ${floor}` : floor}
      </span>
      <span className="sr-only">, worked at least {coarseElapsedLabel(worked)} before it stalled</span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* The number, for the transcript                                       */
/* ------------------------------------------------------------------ */

/**
 * The transcript's version: a line of its own beneath the indicator bubble,
 * the way a timestamp sits beneath a message.
 *
 * The separate line is the point. A pill has only one line, so it has to set
 * the number against whatever the label says — "Making a change in GitHub
 * 1h 12m" — where the digits read as that one step's duration; here they sit
 * under the bubble instead. Both surfaces then attach the state word to the
 * number, which is the half of the treatment a pill can also afford, so
 * neither of them needs to be hovered to be read correctly.
 */
export function TranscriptWorkingTime({
  since,
  worked,
}: {
  /** Null when the turn is working but no stretch can be counted from. Never
   * `undefined` here: a status with no clock at all shows no line, and that
   * decision belongs to the caller, which knows the difference between an
   * API that measured nothing and an API that never sent the field. */
  since: string | null;
  worked: number;
}) {
  // The line, not just its contents, is conditional: an empty one would
  // reserve 20px under the bubble on the render before the browser's clock is
  // readable and give the indicator a shrug on first paint.
  if (since === null) {
    // Measured, and no instant came out of it. The banked seconds are still a
    // real measurement of everything the turn did before it stalled, and
    // "Worked at least 16s" is both true and more use than refusing to say a
    // number the API already computed and sent.
    if (worked > 0) {
      return (
        <p
          data-testid="working-indicator-elapsed-banked"
          title={WORKING_TIME_BANKED_TITLE}
          className={LINE}
        >
          <span aria-hidden className="tabular-nums">Worked at least {elapsedLabel(worked)}</span>
          <span className="sr-only">
            Worked at least {coarseElapsedLabel(worked)} before it stalled.
          </span>
        </p>
      );
    }
    return (
      <p
        data-testid="working-indicator-elapsed-unavailable"
        title={WORKING_TIME_UNAVAILABLE_TITLE}
        className={LINE}
      >
        Working time unavailable
      </p>
    );
  }
  return <CountingTranscriptWorkingTime since={since} worked={worked} />;
}

/** `text-dim` rather than the `text-faint` the timestamps use: at 11px this
 * needs 4.5:1, and faint measures 3.28:1 on the transcript's own background
 * where dim measures 6.39:1. */
const LINE = "mt-1 text-[11px] text-dim";

function CountingTranscriptWorkingTime({ since, worked }: { since: string; worked: number }) {
  const total = useWorkedSeconds(since, worked);
  if (total === null) return null;
  return (
    <p className={LINE} title={WORKING_TIME_TITLE}>
      <span aria-hidden data-testid="working-indicator-elapsed" className="tabular-nums">
        Working {elapsedLabel(total)}
      </span>
      <span className="sr-only">Working for {coarseElapsedLabel(total)} so far.</span>
    </p>
  );
}
