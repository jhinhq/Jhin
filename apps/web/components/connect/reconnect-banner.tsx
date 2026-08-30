"use client";

/** The standing notice that some app's permission has lapsed, and the one
 * button that fixes it.
 *
 * An OAuth token that can no longer be refreshed is not an outage and not a
 * misconfiguration — the setup is intact, the permission is not — so it gets
 * its own status and its own banner rather than being folded into "error".
 * Agents holding a grant to one of these refuse the tool call with the same
 * words, so the fix has to be one click from wherever people already look:
 * the banner, the connection card, and the connection drawer all render the
 * same button rather than three copies of the same mutation.
 *
 * Reconnect re-authorizes the existing row: same connection, same grants,
 * same name, new tokens. Nothing is deleted and nothing has to be re-granted. */

import { AlertTriangle, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useReauthorizeConnection } from "@/lib/hooks";
import { navigateToProvider, needsReauth, saveReturnRoute } from "@/lib/oauth";
import type { ConnectionInfo } from "@/lib/types";

/** Start a re-authorization and hand the browser to the provider. The URL is
 * built by our API from settings; nothing in the request influences it. */
export function ReconnectButton({
  workspaceId,
  connection,
  size = "sm",
  label = "Reconnect",
}: {
  workspaceId: string;
  connection: ConnectionInfo;
  size?: "sm" | "md";
  label?: string;
}) {
  const [leaving, setLeaving] = useState(false);
  const reauthorize = useReauthorizeConnection(workspaceId);

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Button
        type="button"
        size={size}
        variant="primary"
        data-testid={`reconnect-${connection.name}`}
        disabled={reauthorize.isPending || leaving}
        onClick={() => {
          reauthorize.mutate(connection.id, {
            onSuccess: (started) => {
              // Remember where we are so the callback can bring us back.
              setLeaving(true);
              saveReturnRoute();
              navigateToProvider(started.authorization_url);
            },
          });
        }}
      >
        <RefreshCw size={12} aria-hidden />
        {reauthorize.isPending || leaving ? "Starting…" : label}
      </Button>
      {reauthorize.error ? (
        <span role="alert" className="text-right text-xs text-danger">
          {reauthorize.error instanceof ApiError
            ? reauthorize.error.detail
            : "Starting that reconnection failed."}
        </span>
      ) : null}
    </span>
  );
}

export function ReconnectBanner({
  workspaceId,
  connections,
}: {
  workspaceId: string;
  connections: ConnectionInfo[];
}) {
  const stale = needsReauth(connections);
  if (stale.length === 0) return null;

  return (
    <div
      role="status"
      data-testid="reconnect-banner"
      className="space-y-3 rounded-2xl border border-warn/40 bg-warn-soft px-4 py-3.5"
    >
      <p className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
        <AlertTriangle size={15} aria-hidden className="text-warn" />
        {stale.length === 1
          ? "One app needs to be reconnected"
          : `${stale.length} apps need to be reconnected`}
      </p>
      <p className="text-[13px] leading-relaxed text-dim">
        Their sign-in expired or was revoked at the provider. Agents cannot use them until somebody
        signs in again — nothing else about the setup changes.
      </p>
      <ul className="space-y-2">
        {stale.map((connection) => (
          <li
            key={connection.id}
            className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line bg-surface px-3.5 py-2.5"
          >
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-ink">{connection.name}</span>
              <span className="block truncate text-xs text-faint">
                {connection.authorized_by
                  ? `Connected by ${connection.authorized_by.display_name}`
                  : connection.connector_type}
              </span>
            </span>
            <ReconnectButton workspaceId={workspaceId} connection={connection} />
          </li>
        ))}
      </ul>
    </div>
  );
}
