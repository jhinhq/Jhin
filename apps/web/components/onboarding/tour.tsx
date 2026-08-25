"use client";

/**
 * The first-run introduction: one overlay that walks a newcomer through what
 * this product actually is, and — for someone who can configure things — gets
 * the workspace working while they do it.
 *
 * Rules it lives by:
 *
 * - It opens by itself exactly once, for a membership the server still calls
 *   `pending`. Every later visit is on request.
 * - Leaving is always one click (or Escape) away, and is remembered.
 * - Steps report real state rather than asking twice: a workspace that already
 *   has a model shows that step ticked.
 * - What it offers depends on what the person may do. See `steps.ts`.
 */

import { ArrowUpRight, Check, Info } from "lucide-react";
import Link from "next/link";
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSetupStatus } from "@/components/first-run-steps";
import {
  buildOnboardingSteps,
  startingStepIndex,
  type OnboardingStep,
} from "@/components/onboarding/steps";
import { Button, Dialog, focusRing, Spinner } from "@/components/ui";
import { useOnboarding, useSaveOnboarding } from "@/lib/hooks";
import type { OnboardingStatus } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

interface TourControls {
  /** Open the introduction on demand — from Home, or anywhere else. */
  openTour: () => void;
  /** Null until the server has answered, or if it never does. */
  status: OnboardingStatus | null;
}

/** Defaults, not a throw: a screen rendered outside the provider (or in a
 * unit test) should still render, just without a way to open the tour. */
const TourContext = createContext<TourControls>({ openTour: () => {}, status: null });

export function useOnboardingTour(): TourControls {
  return useContext(TourContext);
}

export function OnboardingProvider({ children }: { children: React.ReactNode }) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const state = useOnboarding(workspaceId);
  const save = useSaveOnboarding(workspaceId);
  const [open, setOpen] = useState(false);
  // Auto-open fires at most once per mount, and never again after the person
  // has been shown it — otherwise navigating around the app would keep
  // re-opening the overlay while the write is still in flight.
  const offered = useRef(false);

  const status = state.data?.status ?? null;

  useEffect(() => {
    if (offered.current || status !== "pending") return;
    offered.current = true;
    setOpen(true);
  }, [status]);

  const controls = useMemo<TourControls>(
    () => ({
      openTour: () => {
        offered.current = true;
        setOpen(true);
      },
      status,
    }),
    [status],
  );

  return (
    <TourContext.Provider value={controls}>
      {children}
      {open ? (
        <OnboardingTour
          firstTime={status === "pending" || status === null}
          finished={status === "completed"}
          onLeave={(next, lastStep) => {
            save(next, lastStep);
            setOpen(false);
          }}
        />
      ) : null}
    </TourContext.Provider>
  );
}

function HighlightGrid({ step }: { step: OnboardingStep }) {
  if (!step.highlights) return null;
  return (
    <ul className="mt-4 grid gap-2 sm:grid-cols-2">
      {step.highlights.map((highlight) => {
        const Icon = highlight.icon;
        return (
          <li key={highlight.href}>
            <Link
              href={highlight.href}
              className={`flex h-full gap-3 rounded-xl border border-line bg-raised p-3 transition-colors hover:border-line-strong hover:bg-hover ${focusRing}`}
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-strong">
                <Icon size={16} aria-hidden />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-ink">{highlight.title}</span>
                <span className="block text-xs text-dim">{highlight.body}</span>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function OnboardingTour({
  firstTime,
  finished,
  onLeave,
}: {
  firstTime: boolean;
  /** Already walked to the end once. Revisiting must not undo that. */
  finished: boolean;
  onLeave: (status: OnboardingStatus, lastStep: string | null) => void;
}) {
  const { workspace, can } = useWorkspace();
  const canConfigure = can("admin");
  const setup = useSetupStatus(workspace.workspace_id, canConfigure, { includeChats: true });

  const steps = useMemo(
    () =>
      buildOnboardingSteps({
        workspaceName: workspace.workspace_name,
        canConfigure,
        canChat: can("member"),
        hasModel: setup.hasModel,
        hasAgents: setup.hasAgents,
        hasChats: setup.hasChats,
        hasApps: setup.hasApps,
      }),
    [
      workspace.workspace_name,
      canConfigure,
      can,
      setup.hasModel,
      setup.hasAgents,
      setup.hasChats,
      setup.hasApps,
    ],
  );

  const [index, setIndex] = useState(0);
  const positioned = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Resume where it makes sense, but only once the real state has arrived —
  // otherwise a returning admin would land on "connect a model" purely because
  // the answer had not loaded yet.
  useEffect(() => {
    if (positioned.current || setup.isPending) return;
    positioned.current = true;
    setIndex(startingStepIndex(steps, firstTime));
  }, [setup.isPending, steps, firstTime]);

  const current = steps[Math.min(index, steps.length - 1)];
  const isLast = index >= steps.length - 1;

  useEffect(() => {
    // Move focus to the new step so a screen reader hears it and the keyboard
    // caret does not stay behind on the strip.
    if (positioned.current) headingRef.current?.focus();
  }, [index]);

  return (
    <Dialog title="Getting started" open onClose={() => onLeave("dismissed", current.id)} wide>
      {setup.isPending ? (
        <div className="flex min-h-[16rem] items-center justify-center">
          <Spinner label="Looking at your workspace…" />
        </div>
      ) : (
        <div data-testid="onboarding-tour">
          {/* Wraps rather than scrolls: a step list you have to drag sideways
              to see is a step list nobody reads. */}
          <ol aria-label="Steps" className="flex flex-wrap gap-1.5">
            {steps.map((step, position) => {
              const active = position === index;
              return (
                <li key={step.id}>
                  <button
                    type="button"
                    data-testid={`tour-step-${step.id}`}
                    aria-current={active ? "step" : undefined}
                    onClick={() => setIndex(position)}
                    className={`flex min-h-8 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-medium transition-colors ${focusRing} ${
                      active
                        ? "border-accent bg-accent-soft text-accent-strong"
                        : "border-line bg-raised text-dim hover:text-ink"
                    }`}
                  >
                    {step.done ? (
                      <Check size={13} className="text-ok" aria-hidden />
                    ) : (
                      <span aria-hidden className="tabular-nums text-faint">
                        {position + 1}
                      </span>
                    )}
                    {step.navLabel}
                    {step.done ? <span className="sr-only"> (done)</span> : null}
                  </button>
                </li>
              );
            })}
          </ol>

          <div className="mt-4 min-h-[13rem]">
            <p className="sr-only" aria-live="polite">
              Step {index + 1} of {steps.length}: {current.title}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <h3
                ref={headingRef}
                tabIndex={-1}
                className="font-display text-lg font-semibold tracking-tight outline-none"
              >
                {current.title}
              </h3>
              {current.done ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-ok-soft px-2 py-0.5 text-xs font-medium text-ok">
                  <Check size={12} aria-hidden /> Done
                </span>
              ) : current.optional ? (
                <span className="rounded-full border border-line px-2 py-0.5 text-xs text-faint">
                  Optional
                </span>
              ) : null}
            </div>
            <p className="mt-2 text-sm leading-relaxed text-dim">{current.body}</p>

            {current.blocked ? (
              <p
                data-testid="tour-blocked"
                className="mt-3 flex items-start gap-2 rounded-xl border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn"
              >
                <Info size={15} className="mt-0.5 shrink-0" aria-hidden />
                {current.blocked}
              </p>
            ) : null}

            {current.action ? (
              <div className="mt-4">
                {current.blocked ? (
                  <Button variant="primary" disabled>
                    {current.action.label}
                  </Button>
                ) : (
                  <Link
                    href={current.action.href}
                    // Leaving to go and do the step is not skipping it: the
                    // state is parked so Home can offer to pick it back up.
                    // Somebody who already finished stays finished — coming
                    // back for a link should not reopen their to-do list.
                    onClick={() => onLeave(finished ? "completed" : "in_progress", current.id)}
                  >
                    <Button variant="primary">
                      {current.action.label} <ArrowUpRight size={15} aria-hidden />
                    </Button>
                  </Link>
                )}
              </div>
            ) : null}

            <HighlightGrid step={current} />
          </div>

          <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
            <button
              type="button"
              onClick={() => onLeave("dismissed", current.id)}
              className={`rounded-lg text-sm text-dim underline underline-offset-2 hover:text-ink ${focusRing}`}
            >
              Skip for now
            </button>
            <div className="flex items-center gap-2">
              {index > 0 ? (
                <Button onClick={() => setIndex(index - 1)}>Back</Button>
              ) : null}
              {isLast ? (
                <Button variant="primary" onClick={() => onLeave("completed", current.id)}>
                  Finish
                </Button>
              ) : (
                <Button variant="primary" onClick={() => setIndex(index + 1)}>
                  Next
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
    </Dialog>
  );
}
