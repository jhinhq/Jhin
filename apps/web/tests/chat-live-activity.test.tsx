/**
 * "Working…" for every kind of work, replaced by what the agent is actually
 * doing right now.
 *
 * The sentence is written by the API (see the live-activity contract) — the
 * browser only decides where it beats the generic label. These cover that
 * choice: the waits a person has to act on still win, whitespace is not a
 * label, and the tone/kind never move, so nothing else keyed off them shifts.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveStatusPill } from "@/components/chat/status-pill";
import { Transcript } from "@/components/chat/transcript";
import { composerHintFor, statusLabelFor } from "@/lib/chat";

vi.mock("@/lib/hooks", () => ({}));

afterEach(cleanup);

const SAVING = "Saving this to memory";

describe("statusLabelFor with a live activity", () => {
  it("says what the agent is doing instead of that it is working", () => {
    expect(
      statusLabelFor({
        active_task_state: "running",
        active_run_status: null,
        active_activity: SAVING,
      }),
    ).toEqual({ label: SAVING, tone: "accent", kind: "working", specific: true });
  });

  it("keeps the tone and kind, so everything keyed off them behaves the same", () => {
    const generic = statusLabelFor({ active_task_state: "running", active_run_status: null });
    const specific = statusLabelFor({
      active_task_state: "running",
      active_run_status: null,
      active_activity: "Reading the code",
    });
    expect(specific?.kind).toBe(generic?.kind);
    expect(specific?.tone).toBe(generic?.tone);
    expect(composerHintFor(specific, "Bisby")).toBe(composerHintFor(generic, "Bisby"));
  });

  it("falls back to Working when there is nothing to say", () => {
    for (const activity of [null, "", "   "]) {
      expect(
        statusLabelFor({
          active_task_state: "running",
          active_run_status: null,
          active_activity: activity,
        })?.label,
      ).toBe("Working…");
    }
  });

  it("never speaks over a wait the person has to act on", () => {
    // Progress is something to watch; these are something to do. An activity
    // sentence here would bury the one row that needs the person.
    for (const status of ["waiting_person", "waiting_approval", "waiting_review"]) {
      const live = statusLabelFor({
        active_task_state: "running",
        active_run_status: status,
        active_activity: SAVING,
      });
      expect(live?.label).not.toBe(SAVING);
      expect(live?.kind).not.toBe("working");
    }
  });

  it("says nothing for a task that has not started or has finished", () => {
    expect(
      statusLabelFor({
        active_task_state: "queued",
        active_run_status: null,
        active_activity: SAVING,
      })?.label,
    ).toBe("Waiting for a free slot");
    expect(
      statusLabelFor({
        active_task_state: null,
        active_run_status: null,
        active_activity: SAVING,
      }),
    ).toBeNull();
  });
});

describe("where the activity shows", () => {
  it("carries the sentence into the status pill", () => {
    render(
      <LiveStatusPill
        conversation={{
          active_task_state: "running",
          active_run_status: null,
          active_activity: "Asking a colleague",
        }}
      />,
    );
    const pill = screen.getByTestId("live-status");
    expect(pill.getAttribute("data-kind")).toBe("working");
    expect(pill.textContent).toContain("Asking a colleague");
  });

  it("replaces the transcript's working line with the step it is on", () => {
    render(
      <Transcript
        items={[]}
        agentName="Bisby"
        userName="Varand"
        liveStatus={{ label: SAVING, tone: "accent", kind: "working", specific: true }}
      />,
    );
    const indicator = screen.getByTestId("working-indicator");
    expect(indicator.textContent).toContain(SAVING);
    expect(indicator.textContent).not.toContain("is working");
    // The avatar carries the identity visually; a screen reader gets it too.
    expect(indicator.textContent).toContain("Bisby");
    // Step-by-step churn must not talk over the transcript it sits inside.
    expect(indicator.getAttribute("aria-live")).toBe("off");
  });

  it("leaves the generic working line exactly as it was", () => {
    render(
      <Transcript
        items={[]}
        agentName="Bisby"
        userName="Varand"
        liveStatus={{ label: "Working…", tone: "accent", kind: "working" }}
      />,
    );
    expect(screen.getByTestId("working-indicator").textContent).toContain("Bisby is working…");
  });
});
