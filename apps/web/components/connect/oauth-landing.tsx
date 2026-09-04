"use client";

/** What an OAuth round trip left behind, and the one click that fixes it.
 *
 * The callback's flags used to render as a red line of text above a wall of
 * app cards, which told somebody what happened and then made them go find the
 * thing they had just been trying to connect. This is a card with the retry
 * in it.
 *
 * Two rules it enforces. It never renders a disabled button — a card whose
 * only control is greyed out is the same dead end in a nicer font. And it
 * never renders anything derived from a URL parameter other than the
 * closed-set flag and the pattern-matched connector type: the sentences are
 * Jhin's own, and the provider's words reach this file through nothing. */

import { AlertTriangle } from "lucide-react";
import { ReconnectButton } from "@/components/connect/reconnect-banner";
import { Button } from "@/components/ui";
import type { OAuthLandingCopy } from "@/lib/oauth";
import type { ConnectionInfo, ConnectorInfo } from "@/lib/types";

export function OAuthLandingCard({
  copy,
  connector,
  connection,
  workspaceId,
  onRetry,
  onBrowse,
  onDismiss,
}: {
  copy: OAuthLandingCopy;
  connector: ConnectorInfo | null;
  connection: ConnectionInfo | null;
  workspaceId: string;
  onRetry: (connector: ConnectorInfo) => void;
  onBrowse: () => void;
  onDismiss: () => void;
}) {
  // Declining a permission request is a choice somebody made, not a fault, so
  // it reads neutral. Everything else is a nudge — warn-toned like
  // ReconnectBanner — and never a catastrophe: nothing was connected and
  // nothing was changed in any of these cases.
  const neutral = !copy.operatorHint;
  const tone = neutral
    ? "border-line bg-surface"
    : "border-warn/40 bg-warn-soft";

  return (
    <div
      role="status"
      data-testid="oauth-landing"
      className={`space-y-3 rounded-2xl border px-4 py-3.5 ${tone}`}
    >
      <p className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
        {neutral ? null : <AlertTriangle size={15} aria-hidden className="text-warn" />}
        {copy.title}
      </p>
      <p className="text-[13px] leading-relaxed text-dim">{copy.body}</p>
      {connection !== null ? (
        <p className="text-[13px] leading-relaxed text-dim">
          {connection.name} is still set up — only its sign-in needs redoing.
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {connection !== null ? (
          <ReconnectButton workspaceId={workspaceId} connection={connection} />
        ) : connector !== null ? (
          <Button
            type="button"
            size="sm"
            variant="primary"
            data-testid="oauth-landing-retry"
            onClick={() => onRetry(connector)}
          >
            Connect {connector.display_name} again
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="primary"
            data-testid="oauth-landing-browse"
            onClick={onBrowse}
          >
            Choose an app
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          data-testid="oauth-landing-dismiss"
          onClick={onDismiss}
        >
          Not now
        </Button>
      </div>
      {copy.operatorHint ? (
        <p className="text-[13px] text-faint">
          Nothing is wrong with your account. If this keeps happening, whoever runs this Jhin can
          find the reason in the server log under <code>oauth.callback_refused</code>.
        </p>
      ) : null}
    </div>
  );
}
