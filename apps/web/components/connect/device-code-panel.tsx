"use client";

/** The device-code screen: a code to type somewhere else, and a panel that
 * notices when you have.
 *
 * The alternative to the browser sign-in for a provider that offers it. It
 * needs no redirect URI and no client secret, which is why it is also the
 * first offer when a registration has no secret.
 *
 * The browser never sees the device code. It gets a display code, which is
 * useless on its own, and an opaque handle the server exchanges for it. */

import { CheckCircle2, Copy, ExternalLink, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { Button, ErrorNote, Spinner } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useOAuthDevicePoll } from "@/lib/hooks";
import { formatCountdown, formatUserCode, safeHttpsUrl, secondsUntil } from "@/lib/oauth";
import type { ConnectionInfo, OAuthDeviceStartOut } from "@/lib/types";

/** `https://github.com/login/device` reads better as `github.com/login/device`
 * on a button. Only https becomes a link; anything else stays as text. */
function verificationLabel(uri: string): string | null {
  try {
    const parsed = new URL(uri);
    if (parsed.protocol !== "https:") return null;
    const path = parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/$/, "");
    return `${parsed.host}${path}`;
  } catch {
    return null;
  }
}

export function DeviceCodePanel({
  workspaceId,
  device,
  appSettingsUrl = null,
  onConnected,
  onCancel,
  onRestart,
}: {
  workspaceId: string;
  device: OAuthDeviceStartOut;
  /** GitHub only: where the app has to be installed before the connection
   * reaches a repository. When given, the connected state waits for Done
   * instead of closing at once, so the person sees that sentence. */
  appSettingsUrl?: string | null;
  onConnected: (connection: ConnectionInfo) => void;
  onCancel: () => void;
  /** Where "Try again" goes when the code expires or is declined. Falls back
   * to `onCancel` so the panel is never a dead end without it. */
  onRestart?: () => void;
}) {
  const [remaining, setRemaining] = useState(() => secondsUntil(device.expires_at));
  const [copied, setCopied] = useState(false);
  /* The cadence is the hook's business: it honours the server's own interval
   * and the permanent `slow_down` backoff without this panel re-rendering to
   * change a timer. What lands here is only ever the answer. */
  const poll = useOAuthDevicePoll(
    workspaceId,
    device.handle,
    Math.max(1, device.interval_seconds || 5) * 1000,
  );

  useEffect(() => {
    const tick = () => setRemaining(secondsUntil(device.expires_at));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [device.expires_at]);

  /** The one thing worth telling the caller about: it worked. With an
   * install hint to show, the caller hears it from the Done button instead. */
  useEffect(() => {
    const data = poll.data;
    if (data?.status === "connected" && data.connection && !appSettingsUrl) {
      onConnected(data.connection);
    }
  }, [poll.data, onConnected, appSettingsUrl]);

  const status = poll.data?.status ?? "pending";
  /** A 410 from the poll: the handle is spent or expired server-side. Not a
   * transient failure, so it is not "try again in a moment". */
  const gone = poll.error instanceof ApiError && poll.error.status === 410;
  const expired = status === "expired" || gone || (remaining === 0 && status !== "connected");
  const restart = onRestart ?? onCancel;
  const label = verificationLabel(device.verification_uri);
  const openUrl =
    safeHttpsUrl(device.verification_uri_complete) ?? safeHttpsUrl(device.verification_uri);

  if (status === "connected") {
    const connection = poll.data?.connection ?? null;
    return (
      <div className="space-y-4" data-testid="device-code-panel">
        <p className="flex items-center gap-2 rounded-2xl border border-ok/30 bg-ok-soft px-4 py-3 text-sm text-ok">
          <CheckCircle2 size={16} aria-hidden /> Approved — this app is connected.
        </p>
        {appSettingsUrl ? (
          <>
            <p className="text-[13px] leading-relaxed text-dim" data-testid="device-install-hint">
              Jhin can act as you only in repositories where the app is installed.{" "}
              <a
                href={appSettingsUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-accent-strong hover:underline"
              >
                Open your GitHub Apps <ExternalLink size={11} aria-hidden />
              </a>
              , choose the app, then <span className="font-medium text-ink">Install App</span>.
            </p>
            <div className="flex justify-end">
              <Button
                type="button"
                variant="primary"
                disabled={connection === null}
                onClick={() => {
                  if (connection) onConnected(connection);
                }}
              >
                Done
              </Button>
            </div>
          </>
        ) : null}
      </div>
    );
  }

  if (status === "denied") {
    return (
      <div className="space-y-4" data-testid="device-code-panel">
        <p className="flex items-start gap-2 rounded-2xl border border-danger/30 bg-danger-soft px-4 py-3 text-sm text-danger">
          <XCircle size={16} aria-hidden className="mt-0.5 shrink-0" />
          You declined the request, so nothing was connected.
        </p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel}>
            Close
          </Button>
          <Button type="button" variant="primary" onClick={restart}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  if (expired) {
    return (
      <div className="space-y-4" data-testid="device-code-panel">
        <p className="rounded-2xl border border-warn/30 bg-warn-soft px-4 py-3 text-sm text-warn">
          {gone
            ? "This sign-in attempt is no longer valid. Start again."
            : "That code expired before it was approved. Codes are short-lived on purpose."}
        </p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel}>
            Close
          </Button>
          <Button type="button" variant="primary" onClick={restart}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="device-code-panel">
      <p className="text-sm text-dim">
        Enter this code on the sign-in page. Jhin notices the moment you approve it — leave this
        window open.
      </p>

      <div className="flex flex-col items-center gap-3 rounded-2xl border border-line bg-raised px-4 py-5">
        <p
          data-testid="device-user-code"
          className="select-all font-mono text-3xl font-semibold tracking-[0.25em] text-ink sm:text-4xl"
        >
          {formatUserCode(device.user_code)}
        </p>
        <Button
          size="sm"
          type="button"
          aria-label="Copy the code"
          onClick={() => {
            void navigator.clipboard?.writeText(formatUserCode(device.user_code));
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          <Copy size={12} aria-hidden /> {copied ? "Copied" : "Copy code"}
        </Button>
      </div>

      {openUrl && label ? (
        <a
          href={openUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="btn-gradient inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-xl px-4 text-sm font-semibold"
        >
          <ExternalLink size={14} aria-hidden /> Open {label}
        </a>
      ) : (
        <p className="rounded-xl border border-line bg-raised px-3.5 py-2.5 font-mono text-xs text-ink">
          {device.verification_uri}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Spinner label="Waiting for you to approve it…" />
        <span className="text-xs text-faint" data-testid="device-countdown">
          Expires in {formatCountdown(remaining)}
        </span>
      </div>

      <ErrorNote
        message={
          poll.isError ? "Checking on that code failed. It is still valid — try again." : null
        }
      />

      <div className="flex justify-end">
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
