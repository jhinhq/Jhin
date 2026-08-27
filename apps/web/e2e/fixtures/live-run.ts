/**
 * Keeping a run open long enough to press something.
 *
 * Stop, pause/resume, steering and reload-mid-run all need a run that is still
 * going when the browser gets there, and an ordinary chat turn against the fake
 * provider is over in well under a second.
 *
 * `FAKE_PROVIDER_LATENCY_MS` (compose.dev.yaml) is the documented way to slow
 * one down, but it is set on a container the whole machine shares, so a suite
 * that *depends* on it can only run when someone has re-created that container
 * — and it slows every other run on the stack while it is set.
 *
 * So the window is built out of steps instead: the fake provider turns each
 * `[[tool:...]]` marker in the message into a tool call, and the agent loop
 * pauses, cancels and delivers instructions between steps, which is exactly
 * where the controls need it to be. `system.echo` is the cheapest marker there
 * is — it needs no connection and returns its argument. Latency, when it is
 * set, simply buys the same window with fewer steps.
 */

import type { APIRequestContext } from "@playwright/test";

/** How long the run should stay live, with margin over what a spec spends
 * loading the page, waiting out a 2s poll, and clicking. */
const TARGET_LIVE_MS = 18_000;

/** Measured cost of one echo step end to end (model call, tool worker, the
 * workflow's own bookkeeping) on an instant provider. */
const STEP_OVERHEAD_MS = 380;

/** Two steps still gives the loop a top to come back to, which is where pause,
 * cancellation and pending instructions are picked up. The floor only binds on
 * a provider slow enough that two steps already overrun the window, and going
 * lower there would only make the run longer. */
const MIN_STEPS = 2;
const MAX_STEPS = 60;

/** The published host port of the fake provider (compose.dev.yaml binds
 * 127.0.0.1:8090). Only used to measure it; agents reach it in-network. */
const FAKE_PROVIDER_PROBE_URL =
  process.env.JHIN_E2E_FAKE_PROVIDER_PROBE_URL ?? "http://127.0.0.1:8090/v1/chat/completions";

let measured: Promise<number> | null = null;

/**
 * Round-trip time of one completion, which is `FAKE_PROVIDER_LATENCY_MS` plus
 * a negligible amount of HTTP. Measured once per worker.
 *
 * Unreachable is not fatal: the probe only *sizes* the window, and a stack
 * whose provider port is not published locally (a remote `JHIN_E2E_BASE_URL`,
 * say) still runs everything else. Falling back to the compose default of 0
 * gets the same window out of more steps.
 */
export function providerLatencyMs(request: APIRequestContext): Promise<number> {
  measured ??= (async () => {
    const started = Date.now();
    try {
      const response = await request.post(FAKE_PROVIDER_PROBE_URL, {
        data: { model: "fake-mini", messages: [{ role: "user", content: "latency probe" }] },
        timeout: 30_000,
      });
      if (!response.ok()) throw new Error(`answered ${response.status()}`);
    } catch (cause) {
      console.warn(
        `[e2e] could not measure the fake provider at ${FAKE_PROVIDER_PROBE_URL}` +
          ` (${(cause as Error).message}); assuming FAKE_PROVIDER_LATENCY_MS=0.`,
      );
      return 0;
    }
    return Date.now() - started;
  })();
  return measured;
}

export interface LiveRun {
  /** Message text: the question, followed by the tool script that stretches
   * the run out. Sent verbatim, so keep the question first and readable. */
  text: string;
  /** Steps the agent must be allowed to take (the script, plus its answer). */
  maxSteps: number;
  /** What the agent must be granted to run the script. */
  grants: Record<string, Record<string, unknown>>;
}

/**
 * A turn that keeps the agent busy for roughly `TARGET_LIVE_MS`, sized against
 * whatever latency the fake provider is currently running with.
 */
export function liveRun(question: string, latencyMs: number): LiveRun {
  const perStep = STEP_OVERHEAD_MS + latencyMs;
  const steps = Math.min(MAX_STEPS, Math.max(MIN_STEPS, Math.ceil(TARGET_LIVE_MS / perStep)));
  const script = Array.from(
    { length: steps },
    (_, index) => `[[tool:system.echo {"text": "step ${index + 1}"}]]`,
  ).join(" ");
  return {
    text: `${question} ${script}`,
    // Room for the script and the answer that follows it.
    maxSteps: steps + 5,
    grants: { "system.echo": {} },
  };
}
