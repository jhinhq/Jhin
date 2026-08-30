/** The device-code screen: the code, the wait, and the three ways it ends. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeviceCodePanel } from "@/components/connect/device-code-panel";
import { devicePollDelayMs, SLOW_DOWN_STEP_MS } from "@/lib/oauth";
import type { ConnectionInfo, OAuthDeviceStartOut, OAuthDevicePollOut } from "@/lib/types";

/** What the mocked poll hook returns, and the interval floors it was asked for. */
let pollState: { data: OAuthDevicePollOut | undefined; dataUpdatedAt: number; isError: boolean } = {
  data: undefined,
  dataUpdatedAt: 0,
  isError: false,
};
const intervalsAsked: number[] = [];

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useOAuthDevicePoll: (_workspaceId: string, _handle: string | null, intervalMs: number) => {
      intervalsAsked.push(intervalMs);
      return pollState;
    },
  };
});

const CONNECTION: ConnectionInfo = {
  id: "connection-1",
  connector_type: "github",
  name: "GitHub",
  auth_type: "device_code",
  status: "active",
  public_id: "a".repeat(32),
  config_json: {},
  created_by_user_id: "user-1",
  created_at: "2026-08-29T12:00:00Z",
  last_verified_at: null,
  last_error: null,
  webhook_secret_configured: false,
};

function device(overrides: Partial<OAuthDeviceStartOut> = {}): OAuthDeviceStartOut {
  return {
    handle: "handle-1",
    user_code: "wdjbmjht",
    verification_uri: "https://github.com/login/device",
    verification_uri_complete: null,
    expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    interval_seconds: 5,
    ...overrides,
  };
}

function renderPanel(
  overrides: Partial<OAuthDeviceStartOut> = {},
  handlers: {
    onConnected?: (connection: ConnectionInfo) => void;
    onCancel?: () => void;
    onRestart?: () => void;
  } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onConnected = handlers.onConnected ?? vi.fn();
  const onCancel = handlers.onCancel ?? vi.fn();
  const onRestart = handlers.onRestart ?? vi.fn();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <DeviceCodePanel
        workspaceId="workspace-1"
        device={device(overrides)}
        onConnected={onConnected}
        onCancel={onCancel}
        onRestart={onRestart}
      />
    </QueryClientProvider>,
  );
  return { ...view, onConnected, onCancel, onRestart, queryClient };
}

beforeEach(() => {
  pollState = { data: undefined, dataUpdatedAt: 0, isError: false };
  intervalsAsked.length = 0;
});

afterEach(() => {
  cleanup();
});

describe("DeviceCodePanel", () => {
  it("shows the code the way a person will type it", async () => {
    renderPanel();
    expect((await screen.findByTestId("device-user-code")).textContent).toBe("WDJB-MJHT");
    expect(screen.getByRole("link", { name: /Open github.com\/login\/device/ })).toBeTruthy();
    expect(screen.getByTestId("device-countdown").textContent).toContain("Expires in");
  });

  it("never renders a verification address it would not link to", () => {
    renderPanel({ verification_uri: "javascript:alert(1)" });
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("polls at the server's own interval as a floor", () => {
    renderPanel({ interval_seconds: 7 });
    expect(intervalsAsked[0]).toBe(7000);
  });

  it("raises the cadence on slow_down and never gives it back", () => {
    // The backoff is the poll's own business, so it is asserted where it
    // lives: `slow_down` adds five seconds, and a later `pending` carrying
    // the original interval cannot talk the client back down.
    expect(devicePollDelayMs(5000, "pending", 5, 0)).toBe(5000);
    expect(devicePollDelayMs(5000, "slow_down", 5, SLOW_DOWN_STEP_MS)).toBe(10_000);
    expect(devicePollDelayMs(5000, "pending", 5, SLOW_DOWN_STEP_MS)).toBe(10_000);
    expect(devicePollDelayMs(5000, "pending", 5, 2 * SLOW_DOWN_STEP_MS)).toBe(15_000);
  });

  it("stops polling once there is nothing left to ask about", () => {
    for (const status of ["connected", "denied", "expired"] as const) {
      expect(devicePollDelayMs(5000, status, 5, 0)).toBe(false);
    }
  });

  it("flips to connected in place, without navigating anywhere", async () => {
    const before = window.location.href;
    pollState = {
      data: { status: "connected", interval_seconds: null, connection: CONNECTION },
      dataUpdatedAt: 1,
      isError: false,
    };
    const { onConnected } = renderPanel();
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(CONNECTION));
    expect(screen.getByTestId("device-code-panel").textContent).toContain("Approved");
    // The device flow is the one that never leaves the tab; that is its point.
    expect(window.location.href).toBe(before);
  });

  it("offers a restart when the code expires", async () => {
    pollState = {
      data: { status: "expired", interval_seconds: null, connection: null },
      dataUpdatedAt: 1,
      isError: false,
    };
    const { onRestart } = renderPanel();
    const again = await screen.findByRole("button", { name: "Try again" });
    fireEvent.click(again);
    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  it("says plainly when the request was declined, and offers another go", async () => {
    pollState = {
      data: { status: "denied", interval_seconds: null, connection: null },
      dataUpdatedAt: 1,
      isError: false,
    };
    const { onRestart } = renderPanel();
    expect((await screen.findByTestId("device-code-panel")).textContent).toContain("You declined");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  it("treats a code that ran out of time as expired even before the server says so", async () => {
    renderPanel({ expires_at: new Date(Date.now() - 1000).toISOString() });
    expect((await screen.findByTestId("device-code-panel")).textContent).toContain("expired");
  });
});
