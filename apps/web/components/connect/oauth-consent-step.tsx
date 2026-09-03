"use client";

/** The one screen Jhin shows before the browser leaves for a provider.
 *
 * It exists to say three things that are easy to lose once the provider's own
 * consent page takes over: which authorization server is about to be asked,
 * exactly what is being asked for, and — the part providers never say — that
 * the permission belongs to *this* person and every agent granted the
 * connection will act with it.
 *
 * The scope strings are the server's own words, shown verbatim. Nothing on
 * this card comes from a provider error message. The install hint's link is
 * a table constant validated by the API and rendered only as `https:`; the
 * browser is never navigated to it. */

import { ArrowRight, ExternalLink, ShieldCheck } from "lucide-react";
import { Button, ErrorNote } from "@/components/ui";
import { describeScopes } from "@/lib/oauth";
import type { OAuthProbeOut } from "@/lib/types";

export function OAuthConsentStep({
  appName,
  probe,
  userName,
  busy = false,
  error = null,
  accessSummary,
  installHint,
  onContinue,
  onUseDeviceCode,
  onUseApiKey,
  onCancel,
}: {
  appName: string;
  probe: OAuthProbeOut;
  /** Whose provider account is about to be borrowed. */
  userName: string;
  busy?: boolean;
  error?: string | null;
  /** Replaces the scope list when the provider's access is not a scope
   * string at all — a GitHub App's comes from the permissions it was
   * registered with. */
  accessSummary?: string;
  /** GitHub only: where the app has to be installed before this sign-in can
   * reach a repository. `url` is null when the API had no safe link. */
  installHint?: { url: string | null };
  onContinue: () => void;
  /** The sign-in code, when the provider offers it and a client exists. */
  onUseDeviceCode?: () => void;
  /** The demoted fallback; omitted when there is no API-key scheme to fall
   * back to, so the link is never a dead end. */
  onUseApiKey?: () => void;
  onCancel: () => void;
}) {
  const host = probe.authorization_server_display || probe.issuer || "the provider";
  const linkClass =
    "text-sm text-faint underline underline-offset-2 hover:text-dim disabled:opacity-50";

  return (
    <div className="space-y-4" data-testid="oauth-consent-step">
      <div className="space-y-3 rounded-2xl border border-accent/30 bg-accent-soft px-4 py-3.5">
        <p className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
          <ShieldCheck size={15} aria-hidden /> Connect {appName}
        </p>
        <p className="text-sm leading-relaxed text-ink">
          Jhin will ask <span className="font-medium">{host}</span> for permission to:{" "}
          <span className="font-medium">{accessSummary ?? describeScopes(probe.scopes)}</span>.
        </p>
        <p className="text-[13px] leading-relaxed text-dim">
          You are connecting as <strong className="text-ink">{userName}</strong>. Every agent you
          grant this app to will act with your {appName} permissions.
        </p>
        {installHint ? (
          <p className="text-[13px] leading-relaxed text-dim" data-testid="github-install-hint">
            Jhin cannot see where the app is installed. Until it is installed on an account or
            organization, this sign-in reaches no repositories.{" "}
            {installHint.url ? (
              <a
                href={installHint.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-accent-strong hover:underline"
              >
                Open your GitHub Apps <ExternalLink size={11} aria-hidden />
              </a>
            ) : (
              <span className="text-ink">Open your GitHub Apps</span>
            )}
            , choose the app, then <span className="font-medium text-ink">Install App</span> — for
            an organization&rsquo;s app, start from the organization&rsquo;s Settings → Developer
            settings. You can do this after signing in, too.
          </p>
        ) : null}
      </div>

      {probe.resource ? (
        <p className="text-[13px] text-faint">
          The access is bound to <code className="font-mono text-[12px]">{probe.resource}</code> and
          cannot be used anywhere else.
        </p>
      ) : null}

      <ErrorNote message={error} />

      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="button" variant="primary" onClick={onContinue} disabled={busy}>
          {busy ? "Taking you there…" : `Continue to ${host}`}
          {busy ? null : <ArrowRight size={14} aria-hidden />}
        </Button>
      </div>

      {onUseDeviceCode || onUseApiKey ? (
        <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 border-t border-line pt-3 text-center">
          {onUseDeviceCode ? (
            <button
              type="button"
              data-testid="use-device-link"
              onClick={onUseDeviceCode}
              disabled={busy}
              className={linkClass}
            >
              Sign in with a code instead
            </button>
          ) : null}
          {onUseApiKey ? (
            <button type="button" onClick={onUseApiKey} disabled={busy} className={linkClass}>
              Use an API key instead
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
