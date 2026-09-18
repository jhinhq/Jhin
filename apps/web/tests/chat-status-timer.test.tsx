/**
 * How long the agent has been at it, next to the fact that it is.
 *
 * "Working…" with a pulsing dot says the run has not stopped; it does not say
 * whether that has been four seconds or fourteen minutes, which is the
 * difference between waiting and going to do something else. The API sends
 * the working clock and the browser counts — no request per second, and no
 * timer on the states that are waits on the person rather than progress.
 *
 * The number counts *thinking*. A turn is one run and a run keeps one
 * `started_at` across everything that happens to it, including the hours it
 * spends parked on an approval nobody has looked at, so counting from that
 * stamp shows a person their own deliberation labelled as the agent's. What
 * the API sends instead is a pair — thinking already banked, and the instant
 * the current stretch began — and these tests hold that pair to it.
 *
 * Time is held by hand here rather than by the test runner's timer
 * substitution, and the two things the pill asks the browser for — `Date.now`
 * and `window.setInterval` — are stubbed directly. That is what lets these
 * tests assert the thing the rail actually cares about: how many intervals
 * the page is holding, and that the last unmount gives the final one back.
 */

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveStatusPill } from "@/components/chat/status-pill";
import { Transcript } from "@/components/chat/transcript";
import { coarseElapsedLabel, elapsedLabel, elapsedSeconds, statusLabelFor } from "@/lib/chat";
import type { LiveStatus } from "@/lib/chat";

vi.mock("@/lib/hooks", () => ({}));

const NOW = new Date("2026-09-06T12:00:00Z");
const started = (secondsAgo: number) =>
  new Date(NOW.getTime() - secondsAgo * 1000).toISOString();

/** The clock the component reads, and the intervals it has asked for. */
let currentTime = NOW.getTime();
const intervals = new Map<number, () => void>();
let nextIntervalId = 0;

/** How many repeating callbacks the page is holding right now. */
const intervalCount = () => intervals.size;

/** Move the clock and run every interval that would have come due. */
function advanceTime(milliseconds: number) {
  const ticks = Math.floor(milliseconds / 1000);
  for (let i = 0; i < ticks; i += 1) {
    currentTime += 1000;
    for (const callback of [...intervals.values()]) callback();
  }
}

beforeEach(() => {
  currentTime = NOW.getTime();
  intervals.clear();
  nextIntervalId = 0;
  vi.spyOn(Date, "now").mockImplementation(() => currentTime);
  vi.spyOn(window, "setInterval").mockImplementation(((handler: TimerHandler) => {
    nextIntervalId += 1;
    intervals.set(nextIntervalId, handler as () => void);
    return nextIntervalId;
  }) as typeof window.setInterval);
  vi.spyOn(window, "clearInterval").mockImplementation(((id?: number) => {
    if (id !== undefined) intervals.delete(id);
  }) as typeof window.clearInterval);
});

afterEach(() => {
  // Unmount first: the component gives its interval back on the way out, and
  // that release is one of the things under test.
  cleanup();
  vi.restoreAllMocks();
});

describe("elapsedSeconds", () => {
  it("counts whole seconds from the server's timestamp", () => {
    expect(elapsedSeconds(started(0), NOW.getTime())).toBe(0);
    expect(elapsedSeconds(started(4), NOW.getTime())).toBe(4);
    expect(elapsedSeconds(started(72), NOW.getTime())).toBe(72);
  });

  it("has nothing to count without a usable timestamp", () => {
    expect(elapsedSeconds(null)).toBeNull();
    expect(elapsedSeconds(undefined)).toBeNull();
    expect(elapsedSeconds("")).toBeNull();
    expect(elapsedSeconds("not a date")).toBeNull();
  });

  it("never reads back negative on a browser clock that runs behind", () => {
    // The timestamp is the server's and `now` is the browser's. A machine a
    // few seconds off would otherwise open every timer at "-6s".
    expect(elapsedSeconds(started(-6), NOW.getTime())).toBe(0);
  });
});

describe("elapsedLabel", () => {
  it("is compact enough for a pill in a tight row", () => {
    expect(elapsedLabel(0)).toBe("0s");
    expect(elapsedLabel(4)).toBe("4s");
    expect(elapsedLabel(59)).toBe("59s");
    expect(elapsedLabel(60)).toBe("1m");
    expect(elapsedLabel(72)).toBe("1m 12s");
    expect(elapsedLabel(9 * 60 + 59)).toBe("9m 59s");
    // Past ten minutes the second-by-second digit costs more attention than
    // it returns.
    expect(elapsedLabel(14 * 60 + 30)).toBe("14m");
    expect(elapsedLabel(2 * 3600 + 5 * 60)).toBe("2h 5m");
    expect(elapsedLabel(11 * 3600)).toBe("11h");
  });

  it("rolls over to days, like every other duration in the app", () => {
    // `relativeTime` and `timeAgo` both say "3d". A pill alone in saying
    // "72h" reads as a counter that got stuck rather than as three days.
    expect(elapsedLabel(23 * 3600)).toBe("23h");
    expect(elapsedLabel(24 * 3600)).toBe("1d");
    expect(elapsedLabel(3 * 24 * 3600)).toBe("3d");
    expect(elapsedLabel(3 * 24 * 3600 + 4 * 3600)).toBe("3d 4h");
    // And the same rule that drops seconds past ten minutes drops hours past
    // ten days.
    expect(elapsedLabel(12 * 24 * 3600 + 9 * 3600)).toBe("12d");
  });

  it("never needs more than six characters", () => {
    // What the pill has room for at 320px beside its own label.
    for (const seconds of [0, 9, 59, 72, 599, 3599, 3600 * 9 + 3540, 3600 * 23, 86_400 * 9 + 82_800, 86_400 * 400]) {
      expect(elapsedLabel(seconds).length).toBeLessThanOrEqual(6);
    }
  });
});

describe("statusLabelFor", () => {
  it("carries the working clock only while the agent is genuinely working", () => {
    const since = started(30);
    expect(
      statusLabelFor({
        active_task_state: "running",
        active_run_status: null,
        active_run_working_since: since,
        active_run_working_seconds: 12,
      }),
    ).toMatchObject({ since, worked: 12 });

    // Each of these is a wait, and two of them are waits on the reader. A
    // clock ticking on "Needs your answer" reads as pressure, not progress.
    for (const status of ["waiting_person", "waiting_approval", "waiting_review"]) {
      expect(
        statusLabelFor({
          active_task_state: "running",
          active_run_status: status,
          active_run_working_since: since,
        })?.since,
      ).toBeUndefined();
    }
    for (const state of ["queued", "paused"] as const) {
      expect(
        statusLabelFor({
          active_task_state: state,
          active_run_status: null,
          active_run_working_since: since,
        })?.since,
      ).toBeUndefined();
    }
  });

  it("reads the working clock and never the run's own start", () => {
    // The bug this replaced: one `started_at` per turn, stamped once and kept
    // across an approval left overnight, so the morning read as hours of
    // thought. A surface handed only that stamp counts nothing — and, because
    // this conversation carries no working field at all, says nothing either.
    // It is the shape an API older than the working clock sends, and an older
    // API's silence is not a measurement.
    const status = statusLabelFor({
      active_task_state: "running",
      active_run_status: null,
      active_run_started_at: started(40_000),
    } as Parameters<typeof statusLabelFor>[0]);

    expect(status?.kind).toBe("working");
    expect(status?.since).toBeUndefined();
  });

  it("keeps absent, measured-null and measured apart", () => {
    // Three states, and `??` made them two: it turned "this API never sent
    // the field" into "the API measured and found no instant", so every
    // reader mid-rollout was told their agent was stuck behind an approval
    // nobody on their workspace had opened. Everything downstream — the
    // transcript's line, the pill's tooltip, the deploy plan's promise that
    // an older API costs detail and never a wrong answer — rests on these
    // three staying three.
    const absent = statusLabelFor({ active_task_state: "running", active_run_status: null });
    expect(absent?.since).toBeUndefined();

    const stalled = statusLabelFor({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: null,
      active_run_working_seconds: 16,
    });
    expect(stalled?.since).toBeNull();
    expect(stalled?.worked).toBe(16);

    const counting = statusLabelFor({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(30),
      active_run_working_seconds: 12,
    });
    expect(counting?.since).toBe(started(30));
    expect(counting?.worked).toBe(12);
  });

  it("does not call a colleague's turn this agent's thinking", () => {
    // A run parked on a blocking delegation had no branch here, fell through
    // to `running`, and said "Working…" with a clock ticking beside it while
    // the work sat with somebody else. Same shape of lie as a clock on an
    // approval wait, told about a colleague instead of a person.
    const anonymous = statusLabelFor({
      active_task_state: "running",
      active_run_status: "waiting_delegation",
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });
    expect(anonymous).toMatchObject({
      label: "Waiting for a colleague",
      kind: "waiting_delegation",
      tone: "neutral",
    });
    expect(anonymous?.since).toBeUndefined();

    // The detail endpoint sends the colleague's name as the activity
    // sentence, and naming them beats "a colleague".
    const named = statusLabelFor({
      active_task_state: "running",
      active_run_status: "waiting_delegation",
      active_activity: "Waiting for Linus",
      active_run_working_since: started(72),
    });
    expect(named?.label).toBe("Waiting for Linus");
    expect(named?.kind).toBe("waiting_delegation");
    expect(named?.since).toBeUndefined();
  });
});

describe("LiveStatusPill", () => {
  const pill = (props: Parameters<typeof LiveStatusPill>[0]["conversation"]) =>
    render(<LiveStatusPill conversation={props} />);

  it("says how long this has been going, and keeps counting", () => {
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(4),
    });

    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("4s");

    act(() => {
      advanceTime(3000);
    });
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("7s");
  });

  it("shares one clock however many pills are on the page", () => {
    // A rail shows a pill per conversation. One interval serves all of them,
    // and it stops when the last one goes.
    const first = pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(10),
    });
    const second = pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(200),
    });
    expect(intervalCount()).toBe(1);
    expect(screen.getAllByTestId("live-status-elapsed").map((node) => node.textContent)).toEqual([
      "10s",
      "3m 20s",
    ]);

    act(() => {
      advanceTime(2000);
    });
    expect(screen.getAllByTestId("live-status-elapsed").map((node) => node.textContent)).toEqual([
      "12s",
      "3m 22s",
    ]);

    first.unmount();
    expect(intervalCount()).toBe(1);
    second.unmount();
    expect(intervalCount()).toBe(0);
  });

  it("picks up where the thinking left off after a person was asked", () => {
    // The live shape of run 01a076a5 on this workspace: sixteen seconds of
    // thought, a question, nineteen minutes of the person composing an
    // answer, then thinking again. The nineteen minutes are in neither half
    // of what the API sent, so they are in nothing the pill can show.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(4),
      active_run_working_seconds: 16,
    });

    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("20s");

    act(() => {
      advanceTime(3000);
    });
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("23s");
  });

  it("is a number to read, not a control that looks switched off", () => {
    // `opacity-70` measured 2.68:1 on the pill's own background in the light
    // theme against a 4.5:1 floor for 11px text — and it is this app's word
    // for disabled everywhere else it appears.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(4),
    });

    const elapsed = screen.getByTestId("live-status-elapsed");
    expect(elapsed.className).not.toContain("opacity");
    // The hierarchy the fade was buying comes from weight: the pill is
    // `font-medium`, the number is not.
    expect(elapsed.className).toContain("font-normal");
  });

  it("never truncates its own state word to make room for the timer", () => {
    // At 320px with "1m 12s" beside it, `truncate` on this span produced
    // "Workin…", which reads as a bug rather than as a label out of room.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(72),
    });

    const label = screen.getByText("Working…");
    expect(label.className).not.toContain("truncate");
    expect(label.className).toContain("whitespace-nowrap");

    cleanup();

    // An activity sentence is written to be shortened, and still says what it
    // is on hover, so that one does truncate.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Making a change in GitHub",
      active_run_working_since: started(72),
    });
    const activity = screen.getByText("Making a change in GitHub");
    expect(activity.className).toContain("truncate");
    expect(activity.getAttribute("title")).toBe("Making a change in GitHub");
  });

  it("shows no timer on a wait, and none when the API sent no start time", () => {
    const waiting = pill({
      active_task_state: "running",
      active_run_status: "waiting_person",
      active_run_working_since: started(90),
    });
    expect(screen.getByTestId("live-status").textContent).toContain("Needs your answer");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
    waiting.unmount();

    pill({ active_task_state: "running", active_run_status: null });
    act(() => {
      advanceTime(1000);
    });
    expect(screen.getByTestId("live-status").textContent).toContain("Working…");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
  });

  it("costs a pill with nothing to count nothing per second", () => {
    // Most pills on a rail are idle or waiting. None of them subscribes.
    pill({ active_task_state: "queued", active_run_status: null });
    pill({
      active_task_state: "running",
      active_run_status: "waiting_approval",
      active_run_working_since: started(30),
    });
    expect(intervalCount()).toBe(0);
  });

  it("is not read out every second by a screen reader", () => {
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(3),
    });

    // The pulsing dot already carries "still going", and the pill's own words
    // carry the state. A number reciting itself each second would talk over
    // the conversation it sits beside.
    expect(screen.getByTestId("live-status-elapsed").getAttribute("aria-hidden")).toBe("true");
  });
});

describe("coarseElapsedLabel", () => {
  it("is the same duration in words a screen reader can hear once a minute", () => {
    expect(coarseElapsedLabel(0)).toBe("under a minute");
    expect(coarseElapsedLabel(44)).toBe("under a minute");
    expect(coarseElapsedLabel(59)).toBe("under a minute");
    expect(coarseElapsedLabel(60)).toBe("about a minute");
    expect(coarseElapsedLabel(119)).toBe("about a minute");
    expect(coarseElapsedLabel(120)).toBe("about 2 minutes");
    expect(coarseElapsedLabel(14 * 60)).toBe("about 14 minutes");
    expect(coarseElapsedLabel(3600)).toBe("about an hour");
    expect(coarseElapsedLabel(3600 + 12 * 60)).toBe("about 1 hour 12 minutes");
    expect(coarseElapsedLabel(2 * 3600 + 5 * 60)).toBe("about 2 hours 5 minutes");
    expect(coarseElapsedLabel(24 * 3600)).toBe("about a day");
    expect(coarseElapsedLabel(3 * 24 * 3600 + 4 * 3600)).toBe("about 3 days 4 hours");
  });

  it("says the number the reader can see, not a rounder one", () => {
    // A low-vision reader running magnification with a screen reader gets both
    // at once and they are one fact. Rounding here printed "1h 12m" while
    // saying "about 1 hour 13 minutes", and at the top of the scale "23h"
    // against "about 23 hours 59 minutes" — close enough to be believed, far
    // enough apart to read as two different clocks.
    const sameLeadingUnits = (seconds: number) => [
      elapsedLabel(seconds),
      coarseElapsedLabel(seconds),
    ];

    expect(sameLeadingUnits(3600 + 12 * 60 + 40)).toEqual([
      "1h 12m",
      "about 1 hour 12 minutes",
    ]);
    expect(sameLeadingUnits(23 * 3600 + 59 * 60)).toEqual(["23h", "about 23 hours"]);
    expect(sameLeadingUnits(119)).toEqual(["1m 59s", "about a minute"]);
    expect(sameLeadingUnits(9 * 24 * 3600 + 23 * 3600)).toEqual([
      "9d 23h",
      "about 9 days 23 hours",
    ]);

    // The same drops, in both: minutes go past ten hours and hours past ten
    // days, so neither surface offers a precision the other has given up.
    expect(sameLeadingUnits(11 * 3600 + 30 * 60)).toEqual(["11h", "about 11 hours"]);
    expect(sameLeadingUnits(12 * 24 * 3600 + 9 * 3600)).toEqual(["12d", "about 12 days"]);
  });

  it("never contradicts the digits, at any second of the first day", () => {
    // The property behind the examples above: wherever the spoken version
    // names a unit the visible one also names, it names the same count. The
    // one place they differ is seconds, which are never spoken — the whole
    // reason this function exists — and there "under a minute" contains the
    // visible number instead of arguing with it.
    const UNITS = { s: "second", m: "minute", h: "hour", d: "day" } as const;
    // "about a minute" and "about 1 minute" are the same claim; only the count
    // is under test here.
    const counted = (spoken: string) =>
      spoken
        .replace("about a minute", "about 1 minute")
        .replace("about an hour", "about 1 hour")
        .replace("about a day", "about 1 day");

    for (let seconds = 0; seconds <= 25 * 3600; seconds += 7) {
      const seen = elapsedLabel(seconds);
      const spoken = counted(coarseElapsedLabel(seconds));
      const leading = /^(\d+)([smhd])/.exec(seen);
      if (!leading) throw new Error(`unreadable elapsed label: ${seen}`);
      const [, count, unit] = leading;
      if (unit === "s") expect(spoken).toBe("under a minute");
      else expect(spoken).toContain(`${count} ${UNITS[unit as keyof typeof UNITS]}`);

      const trailing = /[hd] (\d+)([mh])$/.exec(seen);
      if (trailing) {
        expect(spoken).toContain(`${trailing[1]} ${UNITS[trailing[2] as keyof typeof UNITS]}`);
      }
    }
  });

  it("holds still for a minute at a time, so nothing recites itself", () => {
    // The visible label moves every second under ten minutes. This one moves
    // once the first whole minute is up and once a minute after that, which is
    // what makes it safe to expose to a screen reader at all.
    const changesAt: number[] = [];
    let previous = coarseElapsedLabel(0);
    for (let seconds = 1; seconds <= 3 * 3600; seconds += 1) {
      const spoken = coarseElapsedLabel(seconds);
      if (spoken !== previous) changesAt.push(seconds);
      previous = spoken;
    }

    expect(changesAt[0]).toBe(60);
    for (let i = 1; i < changesAt.length; i += 1) {
      expect(changesAt[i] - changesAt[i - 1]).toBe(60);
    }
  });
});

describe("the transcript's working indicator", () => {
  const indicator = (status: LiveStatus) =>
    render(
      <Transcript items={[]} agentName="Bisby" userName="Ada" liveStatus={status} />,
    );

  const working = (extra: Partial<LiveStatus> = {}): LiveStatus => ({
    label: "Working…",
    tone: "accent",
    kind: "working",
    ...extra,
  });

  it("says how long, where the operator asked for it", () => {
    // "Bisby is working…" above the composer is the phrase they quoted. The
    // number was built into the header pill and the rail and never landed
    // here, which is the one place a reader watching the agent is looking.
    indicator(working({ since: started(4), worked: 12 }));

    expect(screen.getByTestId("working-indicator").textContent).toContain("Bisby is working…");
    expect(screen.getByTestId("working-indicator-elapsed").textContent).toBe("Working 16s");

    act(() => {
      advanceTime(3000);
    });
    expect(screen.getByTestId("working-indicator-elapsed").textContent).toBe("Working 19s");
  });

  it("keeps the number off the step's sentence, and says what it measures", () => {
    // "Making a change in GitHub · 1h 12m" reads as that step taking an hour
    // and a quarter. It is the whole turn's thinking. Here the number is on
    // its own line under the bubble, carrying the state word it belongs to,
    // and hover says the rest.
    indicator(
      working({
        label: "Making a change in GitHub",
        specific: true,
        since: started(72),
        worked: 4260,
      }),
    );

    const elapsed = screen.getByTestId("working-indicator-elapsed");
    expect(elapsed.textContent).toBe("Working 1h 12m");
    expect(elapsed.closest("[title]")?.getAttribute("title")).toContain("every step of it added up");
    expect(elapsed.closest("[title]")?.getAttribute("title")).toContain("waiting on you");
    // The sentence and the number are not in one run of text.
    expect(screen.getByText("Making a change in GitHub").contains(elapsed)).toBe(false);
  });

  it("gives a screen reader the one fact this feature adds", () => {
    // Not the ticking digits — those stay hidden, because a number reciting
    // itself every second would talk over the transcript. A coarse value that
    // moves once a minute instead.
    indicator(working({ since: started(4), worked: 3600 }));

    expect(screen.getByTestId("working-indicator-elapsed").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("Working for about an hour so far.")).toBeTruthy();
  });

  it("spends the figure the API already measured instead of refusing to", () => {
    // A turn whose clock stopped: an approval, question or review nobody
    // closed, or a run that finished under a task still reading as running.
    // The clock cannot resume — there is no instant to count from — but the
    // seconds it banked before it stopped are a real measurement the API
    // computed and sent, and "at least 16s" is both true and more use than
    // "unavailable".
    indicator(working({ since: null, worked: 16 }));

    expect(screen.queryByTestId("working-indicator-elapsed")).toBeNull();
    expect(screen.queryByTestId("working-indicator-elapsed-unavailable")).toBeNull();
    const banked = screen.getByTestId("working-indicator-elapsed-banked");
    expect(banked.textContent).toContain("Worked at least 16s");
    expect(banked.getAttribute("title")).toContain("nobody closed");
    expect(banked.getAttribute("title")).toContain("floor");
    // Nothing is ticking, so nothing subscribes to the page's clock.
    expect(intervalCount()).toBe(0);
  });

  it("says why there is no clock when there is not even a figure", () => {
    // Parked before it worked a whole second. Silence looked like the feature
    // had never shipped, and the live database already has such a row.
    indicator(working({ since: null, worked: 0 }));

    expect(screen.queryByTestId("working-indicator-elapsed-banked")).toBeNull();
    const note = screen.getByTestId("working-indicator-elapsed-unavailable");
    expect(note.textContent).toBe("Working time unavailable");
    expect(note.getAttribute("title")).toContain("nobody closed");
  });

  it("offers the causes of a missing clock instead of asserting one", () => {
    // `working_since` comes back null for a turn parked on a wait nobody
    // closed, for a run that has already finished, for a run whose span was
    // never usable, and for a task still marked running with no run behind it
    // — and the projection sends the same null for all of them. A tooltip
    // that stated the first as fact ("This reply paused for an approval,
    // question or review that was never closed") was telling three of those
    // four readers something about their own workspace that is not true.
    indicator(working({ since: null, worked: 0 }));
    const unavailable =
      screen.getByTestId("working-indicator-elapsed-unavailable").getAttribute("title") ?? "";
    cleanup();
    indicator(working({ since: null, worked: 16 }));
    const banked =
      screen.getByTestId("working-indicator-elapsed-banked").getAttribute("title") ?? "";

    for (const title of [unavailable, banked]) {
      // Nothing is asserted to have happened...
      expect(title).not.toContain("This reply paused for");
      // ...and more than one thing is offered as what might have.
      expect(title).toContain("or");
      expect(title).toContain("approval, question or review");
    }
    // With nothing banked, "no start recorded" is one of the live
    // possibilities and the wording has to leave room for it.
    expect(unavailable).toContain("no start recorded");
    // With seconds banked it cannot be that, but the run may simply have
    // ended, so the pause is still not the only candidate.
    expect(banked).toContain("end of the run");
  });

  it("shows nothing at all when the API never sent a working clock", () => {
    // The older-API shape, which is a different fact from an API that
    // measured and came up empty: nothing was measured, so there is nothing
    // to report — no number, no note, no tooltip blaming a pause that never
    // happened. This is what `docs/deployment.md` step 6 promises a
    // mid-rollout reader.
    indicator(working());

    expect(screen.queryByTestId("working-indicator-elapsed")).toBeNull();
    expect(screen.queryByTestId("working-indicator-elapsed-banked")).toBeNull();
    expect(screen.queryByTestId("working-indicator-elapsed-unavailable")).toBeNull();
    expect(screen.getByTestId("working-indicator").textContent).not.toContain("unavailable");
  });

  it("keeps no clock on a wait", () => {
    indicator({ label: "Needs your answer", tone: "warn", kind: "question", since: started(90) });

    expect(screen.queryByTestId("working-indicator-elapsed")).toBeNull();
    expect(intervalCount()).toBe(0);
  });

  it("shares the page's one clock with the header pill", () => {
    // The open thread shows both at once. Two timers for one number would be
    // two intervals for one number.
    render(
      <LiveStatusPill
        conversation={{
          active_task_state: "running",
          active_run_status: null,
          active_run_working_since: started(10),
        }}
      />,
    );
    indicator(working({ since: started(10) }));

    expect(intervalCount()).toBe(1);
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("10s");
    expect(screen.getByTestId("working-indicator-elapsed").textContent).toBe("Working 10s");
  });

  it("is legible: the elapsed line is not the app's faint ink", () => {
    // 11px needs 4.5:1. `text-faint` — what the timestamps beside it use —
    // measures 3.28:1 on the transcript's own background; `text-dim` measures
    // 6.39:1. The number and both of the lines that stand in for it are read.
    indicator(working({ since: started(4) }));
    const counting = screen.getByTestId("working-indicator-elapsed").closest("p");
    cleanup();
    indicator(working({ since: null, worked: 4 }));
    const banked = screen.getByTestId("working-indicator-elapsed-banked");
    cleanup();
    indicator(working({ since: null, worked: 0 }));
    const unavailable = screen.getByTestId("working-indicator-elapsed-unavailable");

    for (const line of [counting, banked, unavailable]) {
      expect(line?.className).toContain("text-dim");
      expect(line?.className).not.toContain("text-faint");
    }
  });

  it("names the colleague a delegated turn is parked on, and starts no clock", () => {
    indicator({
      label: "Waiting for Linus",
      tone: "neutral",
      kind: "waiting_delegation",
      specific: true,
    });

    const note = screen.getByTestId("working-indicator").textContent ?? "";
    expect(note).toContain("Waiting for Linus");
    expect(note).toContain("Bisby picks this up again when they reply");
    expect(note).not.toContain("paused");
    expect(screen.queryByTestId("working-indicator-elapsed")).toBeNull();
    expect(intervalCount()).toBe(0);
  });

  it("still says what a delegated turn is waiting on without the name", () => {
    // The rail's shape: the chat list does not pay for an activity sentence
    // per row, so there is no colleague to name.
    indicator({ label: "Waiting for a colleague", tone: "neutral", kind: "waiting_delegation" });

    expect(screen.getByTestId("working-indicator").textContent).toContain(
      "Bisby is waiting for a colleague",
    );
  });
});

describe("what the pill says when the digits are hidden", () => {
  const pill = (props: Parameters<typeof LiveStatusPill>[0]["conversation"]) =>
    render(<LiveStatusPill conversation={props} />);

  it("offers a coarse value in place of the number it hides", () => {
    // `aria-hidden` on a ticking number is right; `aria-hidden` and nothing
    // else left the one fact this feature adds unavailable to a screen
    // reader, whose whole accessible name was "Working…".
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });

    const node = screen.getByTestId("live-status");
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("1h 12m");
    expect(node.textContent).toContain(", working for about 1 hour 12 minutes so far");
  });

  it("says what the number measures on hover, where there is no room to write it", () => {
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Making a change in GitHub",
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });

    const title = screen.getByTestId("live-status-elapsed").closest("[title]")?.getAttribute("title");
    expect(title).toContain("every step of it added up");
  });

  it("carries its own state word when it sits against a step sentence", () => {
    // "Making a change in GitHub" then "1h 12m", with nothing between them,
    // reads as that step taking an hour and a quarter. It is the whole turn's
    // thinking. The transcript separates the two onto different lines; a pill
    // has one line, so the number takes the word with it and names its own
    // subject without waiting to be hovered — which a touch reader never
    // does.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Making a change in GitHub",
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("Working 1h 12m");
    cleanup();

    // Against the generic label the number is already attached to the word it
    // measures, and repeating it would stutter.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });
    expect(screen.getByTestId("live-status-elapsed").textContent).toBe("1h 12m");
    expect(screen.getByTestId("live-status").textContent).toContain("Working…");
  });

  it("marks where the step sentence ends and the number begins", () => {
    // The state word alone was not enough. Two independent phrases at 11px in
    // one colour, separated by the pill's 6px gap against a 3.09px space,
    // arrive as "Making a change in GitHub Working 1h 12m" — an ungrammatical
    // run-on that only comes apart into two facts on a second read.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Making a change in GitHub",
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });

    const mark = screen.getByTestId("live-status-phrase-break");
    expect(mark.textContent).toBe("·");
    // Punctuation for the eye: the accessible name separates the same two
    // phrases with a comma of its own, and has no use for a spoken "middle
    // dot".
    expect(mark.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByTestId("live-status").textContent).toContain(
      ", working for about 1 hour 12 minutes so far",
    );
    // It sits between them, not after the number.
    expect(mark.compareDocumentPosition(screen.getByTestId("live-status-elapsed"))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    cleanup();

    // A stopped clock beside a step sentence is two phrases just the same.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Making a change in GitHub",
      active_run_working_since: null,
      active_run_working_seconds: 16,
    });
    expect(screen.getByTestId("live-status-phrase-break")).toBeTruthy();
    expect(screen.getByTestId("live-status-elapsed-banked").textContent).toBe("Worked ≥16s");
    cleanup();

    // "Working… 1h 12m" is one phrase whose ellipsis already breaks it. A dot
    // there would cut a sentence in half.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });
    expect(screen.queryByTestId("live-status-phrase-break")).toBeNull();
  });

  it("does not go quiet when there is no clock to count", () => {
    // The rail and the header have no room for the explanation the transcript
    // gives, but "no number and no word" is what made the feature look absent.
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: null,
    });

    const node = screen.getByTestId("live-status");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
    expect(node.textContent).toContain(", working time unavailable");
    expect(node.getAttribute("title")).toContain("nobody closed");
  });

  it("shows the figure it did measure, marked as a floor", () => {
    pill({
      active_task_state: "running",
      active_run_status: null,
      active_run_working_since: null,
      active_run_working_seconds: 16,
    });

    const node = screen.getByTestId("live-status");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
    expect(screen.getByTestId("live-status-elapsed-banked").textContent).toBe("≥16s");
    expect(node.textContent).toContain(", worked at least under a minute before it stalled");
    expect(node.getAttribute("title")).toContain("floor");
    expect(intervalCount()).toBe(0);
  });

  it("says nothing at all to a reader whose API never sent the clock", () => {
    // The whole of W1 in one render: a conversation with neither working
    // field is an API older than the feature, not a stalled approval. It gets
    // no number, no note, and above all no tooltip telling a reader that a
    // pause nobody opened on their workspace is holding their agent up.
    pill({ active_task_state: "running", active_run_status: null });

    const node = screen.getByTestId("live-status");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
    expect(screen.queryByTestId("live-status-elapsed-banked")).toBeNull();
    expect(node.textContent).toBe("Working…");
    expect(node.getAttribute("title")).toBeNull();
  });

  it("shows no clock and no stall note on a turn parked with a colleague", () => {
    pill({
      active_task_state: "running",
      active_run_status: "waiting_delegation",
      active_activity: "Waiting for Linus",
      active_run_working_since: started(72),
      active_run_working_seconds: 4260,
    });

    const node = screen.getByTestId("live-status");
    expect(node.getAttribute("data-kind")).toBe("waiting_delegation");
    expect(node.textContent).toContain("Waiting for Linus");
    expect(screen.queryByTestId("live-status-elapsed")).toBeNull();
    expect(screen.queryByTestId("live-status-elapsed-banked")).toBeNull();
    expect(node.getAttribute("title")).toBeNull();
    expect(intervalCount()).toBe(0);
  });
});
