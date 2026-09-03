"use client";

/** A provider at a glance, as one row: who it is, whether it works, how
 * many models run on it, and what it has cost this month. Everything else —
 * endpoint, credential, verify, edit, delete — sits behind Manage so the
 * list stays a status board, not a control panel. An Ollama host's live
 * state (what is loaded, Load and Unload) has its own Local models block on
 * the page; here it is a row like any other. */

import { LogoTile } from "@/components/catalog/logo-tile";
import { Button, StatusLabel } from "@/components/ui";
import { formatMicrosAsDollars } from "@/lib/models";
import type { ModelProvider } from "@/lib/types";

/** Colored dot + words, never color alone: ok while it verified cleanly,
 * danger once the provider reported an error, neutral when switched off. */
export function ProviderStatus({ provider }: { provider: ModelProvider }) {
  if (!provider.enabled) return <StatusLabel tone="neutral">Turned off</StatusLabel>;
  if (provider.last_error) return <StatusLabel tone="danger">Needs attention</StatusLabel>;
  return <StatusLabel tone="ok">Connected</StatusLabel>;
}

export function ProviderCard({
  provider,
  typeLabel,
  profileCount,
  spentMonthMicros,
  onManage,
}: {
  provider: ModelProvider;
  /** Human name of the provider type ("OpenAI", "Ollama (local)", …). */
  typeLabel: string;
  profileCount: number;
  /** Tracked spend on this provider this month; shown only when non-zero. */
  spentMonthMicros?: number;
  onManage: () => void;
}) {
  return (
    <li
      data-testid={`provider-card-${provider.id}`}
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-3 md:px-5"
    >
      <LogoTile name={provider.display_name} size={36} />
      <div className="min-w-0 flex-1 basis-40">
        <h3 className="truncate font-display text-sm font-semibold text-ink">
          {provider.display_name}
        </h3>
        <p className="truncate text-xs text-faint">{typeLabel}</p>
      </div>
      <ProviderStatus provider={provider} />
      <span className="text-xs tabular-nums text-faint">
        {profileCount} {profileCount === 1 ? "model" : "models"}
      </span>
      {spentMonthMicros !== undefined && spentMonthMicros > 0 ? (
        <span className="text-xs tabular-nums text-faint">
          {formatMicrosAsDollars(spentMonthMicros)} this month
        </span>
      ) : null}
      <Button size="sm" variant="ghost" className="ml-auto" onClick={onManage}>
        Manage
      </Button>
    </li>
  );
}
