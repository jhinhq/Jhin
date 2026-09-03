"use client";

/** A provider at a glance: who it is, whether it works, and how many models
 * run on it. Everything else — endpoint, credential, verify, edit, delete —
 * sits behind Manage so the grid stays a status board, not a control panel.
 *
 * An Ollama host is the one deliberate exception. What is loaded in its
 * memory changes by the minute and is the thing an admin comes here to check
 * and act on, so its card carries a live status line in the header and the
 * local-models panel in the body. Both used to sit behind Manage, and people
 * could not find them. */

import { useState } from "react";
import { Button, ErrorNote, StatusLabel } from "@/components/ui";
import { LogoTile } from "@/components/catalog/logo-tile";
import { OllamaPanel } from "@/components/models/ollama-panel";
import { ollamaLoadedSummary, type ProfilePrefill } from "@/lib/models";
import type { OllamaHost } from "@/lib/ollama-host";
import type { ModelProvider } from "@/lib/types";

/** Colored dot + words, never color alone: ok while it verified cleanly,
 * danger once the provider reported an error, neutral when switched off. */
export function ProviderStatus({ provider }: { provider: ModelProvider }) {
  if (!provider.enabled) return <StatusLabel tone="neutral">Turned off</StatusLabel>;
  if (provider.last_error) return <StatusLabel tone="danger">Needs attention</StatusLabel>;
  return <StatusLabel tone="ok">Connected</StatusLabel>;
}

/** What an Ollama card needs beyond the provider row. */
export interface OllamaCardProps {
  /** The page's subscription to this host — see lib/ollama-host.ts. */
  host: OllamaHost;
  isAdmin: boolean;
  /** Opens the new-profile dialog prefilled for a picked local model. */
  onUseAsModel: (prefill: ProfilePrefill) => void;
}

const SUMMARY_TONE = {
  ok: "text-ok",
  neutral: "text-faint",
  danger: "text-danger",
} as const;

/** One line under the type label saying what is resident right now, so the
 * state reads before any scrolling. It comes from the ten-second poll, not
 * the listing, so it stays true when the panel below cannot list the host. */
function OllamaHeaderStatus({ host }: { host: OllamaHost }) {
  const summary = ollamaLoadedSummary(
    host.loaded.data,
    host.loaded.isError,
    host.models.data?.models.length ?? null,
  );
  if (!summary) {
    return (
      <p data-testid="ollama-header-status" className="truncate text-xs text-faint">
        Checking what&apos;s loaded…
      </p>
    );
  }
  return (
    <p
      data-testid="ollama-header-status"
      title={summary.detail ?? summary.text}
      className={`truncate text-xs ${SUMMARY_TONE[summary.tone]}`}
    >
      {summary.text}
    </p>
  );
}

export function ProviderCard({
  provider,
  typeLabel,
  profileCount,
  onManage,
  ollama,
}: {
  provider: ModelProvider;
  /** Human name of the provider type ("OpenAI", "Ollama (local)", …). */
  typeLabel: string;
  profileCount: number;
  onManage: () => void;
  /** Present only for an Ollama provider; a cloud card stays a status tile. */
  ollama?: OllamaCardProps;
}) {
  const [panelError, setPanelError] = useState<string | null>(null);

  // The panel's rows put facts on the left and buttons on the right, a shape
  // made for the manage dialog's 672px. Two of the three xl columns is about
  // that width. One column (~330px) folds every row's buttons under its
  // facts and makes a four-model host three cards tall; all three columns
  // strands the buttons a screen away from the names. At md the grid has two
  // columns, so the card is simply full width there.
  const span = ollama ? "md:col-span-2" : "";

  return (
    <article
      data-testid={`provider-card-${provider.id}`}
      className={`flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card ${span}`}
    >
      <header className="flex items-center gap-3">
        <LogoTile name={provider.display_name} size={40} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-display text-sm font-semibold text-ink">
            {provider.display_name}
          </h3>
          <p className="truncate text-xs text-dim">{typeLabel}</p>
          {ollama ? <OllamaHeaderStatus host={ollama.host} /> : null}
        </div>
      </header>
      <div className="flex items-center justify-between gap-2">
        <ProviderStatus provider={provider} />
        <span className="text-xs text-faint">
          {profileCount} {profileCount === 1 ? "model" : "models"}
        </span>
      </div>
      {ollama ? (
        <>
          <OllamaPanel
            provider={provider}
            host={ollama.host}
            isAdmin={ollama.isAdmin}
            onError={setPanelError}
            onUseAsModel={ollama.onUseAsModel}
          />
          <ErrorNote message={panelError} />
        </>
      ) : null}
      <footer className="mt-auto flex justify-end border-t border-line pt-3">
        <Button size="sm" variant="ghost" onClick={onManage}>
          Manage
        </Button>
      </footer>
    </article>
  );
}
