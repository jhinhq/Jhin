"use client";

/** A provider at a glance: who it is, whether it works, and how many models
 * run on it. Everything else — endpoint, credential, balance, verify, edit,
 * delete — sits behind Manage so the grid stays a status board, not a
 * control panel. */

import { Button, StatusLabel } from "@/components/ui";
import { LogoTile } from "@/components/catalog/logo-tile";
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
  onManage,
}: {
  provider: ModelProvider;
  /** Human name of the provider type ("OpenAI", "Ollama (local)", …). */
  typeLabel: string;
  profileCount: number;
  onManage: () => void;
}) {
  return (
    <article
      data-testid={`provider-card-${provider.id}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <header className="flex items-center gap-3">
        <LogoTile name={provider.display_name} size={40} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-display text-sm font-semibold text-ink">
            {provider.display_name}
          </h3>
          <p className="truncate text-xs text-dim">{typeLabel}</p>
        </div>
      </header>
      <div className="flex items-center justify-between gap-2">
        <ProviderStatus provider={provider} />
        <span className="text-xs text-faint">
          {profileCount} {profileCount === 1 ? "model" : "models"}
        </span>
      </div>
      <footer className="mt-auto flex justify-end border-t border-line pt-3">
        <Button size="sm" variant="ghost" onClick={onManage}>
          Manage
        </Button>
      </footer>
    </article>
  );
}
