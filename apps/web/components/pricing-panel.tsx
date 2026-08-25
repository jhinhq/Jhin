"use client";

/**
 * "Where prices come from" — the panel that makes Jhin's pricing legible.
 *
 * Every price on the Models page comes from one of five places, and which one
 * matters: a list price from a stale catalog and a rate measured off the
 * workspace's own invoice deserve very different amounts of trust. This panel
 * states the current situation, offers the two actions that improve it, and
 * names anything Jhin still cannot price.
 */

import { RefreshCw, Sigma, TriangleAlert } from "lucide-react";
import { Badge, Button, ErrorNote, Spinner } from "@/components/ui";
import { catalogStalenessNote, formatMicrosAsDollars } from "@/lib/models";
import type { CatalogRefreshResult, PricingStatus, ReconcilePricingResult } from "@/lib/types";

export const PRECEDENCE_EXPLAINER =
  "When sources disagree Jhin uses, in order: a price you entered, a rate measured " +
  "from your real spend, a live price from the provider, the community-maintained " +
  "catalog, then the list prices built into this release. Community and list prices " +
  "can be stale or wrong, which is exactly why they rank last.";

/** Fallback notice if the API ever omits one; the server's copy wins. */
export const LITELLM_ATTRIBUTION =
  "LiteLLM model price map, MIT License, Copyright (c) 2023 Berri AI";
export const LITELLM_PROJECT_URL = "https://github.com/BerriAI/litellm";

function formatFetched(iso: string | null): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "never" : parsed.toISOString().slice(0, 10);
}

export interface PricingPanelProps {
  status: PricingStatus | undefined;
  isPending: boolean;
  isAdmin: boolean;
  onReconcile: () => void;
  onRefreshCatalog: () => void;
  reconciling: boolean;
  refreshing: boolean;
  reconcileResult: ReconcilePricingResult | null;
  catalogResult: CatalogRefreshResult | null;
  error: string | null;
}

export function PricingPanel({
  status,
  isPending,
  isAdmin,
  onReconcile,
  onRefreshCatalog,
  reconciling,
  refreshing,
  reconcileResult,
  catalogResult,
  error,
}: PricingPanelProps) {
  if (isPending) return <Spinner label="Loading pricing sources…" />;
  if (!status) return null;

  const staleness = status.catalog_stale ? catalogStalenessNote(status.catalog_updated) : null;
  const unpriced = status.profiles.filter((profile) => !profile.priced);
  const measured = status.profiles.filter((profile) => profile.observed !== null);

  return (
    <section
      data-testid="pricing-panel"
      aria-label="Where prices come from"
      className="rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-sm font-semibold text-ink">Where prices come from</h3>
          <p className="mt-1 max-w-2xl text-xs text-dim">{PRECEDENCE_EXPLAINER}</p>
        </div>
        {isAdmin ? (
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              onClick={onRefreshCatalog}
              disabled={refreshing}
              title="Fetch the LiteLLM community price map"
            >
              <RefreshCw aria-hidden className="mr-1 h-3.5 w-3.5" />
              {refreshing ? "Refreshing…" : "Refresh price catalog"}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={onReconcile}
              disabled={reconciling || !status.reconcile_available}
              title={status.reconcile_detail}
            >
              <Sigma aria-hidden className="mr-1 h-3.5 w-3.5" />
              {reconciling ? "Measuring…" : "Measure real rates"}
            </Button>
          </div>
        ) : null}
      </div>

      <ErrorNote message={error} />

      <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-faint">Built-in list prices</dt>
          <dd className="text-dim">Catalog {status.catalog_updated}</dd>
        </div>
        <div>
          <dt className="text-faint">Community catalog</dt>
          <dd className="text-dim">
            {status.refreshed_source ? (
              <>
                <span>
                  {status.refreshed_entry_count} prices, refreshed{" "}
                  {formatFetched(status.refreshed_fetched_at)} from{" "}
                  <a
                    href={status.refreshed_project_url || LITELLM_PROJECT_URL}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="underline underline-offset-2"
                  >
                    LiteLLM
                  </a>{" "}
                  (MIT)
                </span>
                <span
                  data-testid="catalog-attribution"
                  className="mt-0.5 block text-[11px] text-faint"
                >
                  {status.refreshed_attribution ?? LITELLM_ATTRIBUTION}
                </span>
              </>
            ) : (
              <>
                Never refreshed — pulls community-maintained prices from{" "}
                <a
                  href={status.refreshed_project_url || LITELLM_PROJECT_URL}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="underline underline-offset-2"
                >
                  LiteLLM
                </a>{" "}
                (MIT)
              </>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-faint">Measured from your spend</dt>
          <dd className="text-dim">
            {measured.length > 0
              ? `${measured.length} model${measured.length === 1 ? "" : "s"}`
              : status.reconcile_available
                ? "Not measured yet"
                : "Unavailable"}
          </dd>
        </div>
      </dl>

      {staleness ? (
        <p
          data-testid="catalog-staleness"
          className="mt-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-warn"
        >
          <TriangleAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {staleness}
            {isAdmin ? " Refreshing the price catalog pulls current numbers." : ""}
          </span>
        </p>
      ) : null}

      {!status.reconcile_available ? (
        <p className="mt-3 text-xs text-faint">{status.reconcile_detail}</p>
      ) : null}

      {unpriced.length > 0 ? (
        <p data-testid="pricing-unpriced-summary" className="mt-3 text-xs text-warn">
          {unpriced.length} model{unpriced.length === 1 ? " has" : "s have"} no price, so spend for{" "}
          {unpriced.length === 1 ? "it isn't" : "them isn't"} tracked:{" "}
          {unpriced.map((profile) => profile.model_name).join(", ")}.
        </p>
      ) : null}

      {catalogResult ? (
        <p data-testid="catalog-refresh-result" className="mt-3 text-xs text-dim">
          {catalogResult.detail}
        </p>
      ) : null}

      {reconcileResult ? (
        <div data-testid="reconcile-result" className="mt-3 space-y-2 text-xs">
          <p className="text-dim">{reconcileResult.detail}</p>
          {reconcileResult.providers.map((provider) => (
            <div key={provider.provider_id} className="rounded-md border border-line px-3 py-2">
              <p className="font-medium text-ink">{provider.display_name}</p>
              <p className="text-dim">{provider.detail}</p>
              {provider.derived.map((rate) => (
                <p key={rate.model_key} className="mt-1 text-dim">
                  <code className="font-mono">{rate.model_key}</code>{" "}
                  <Badge tone={rate.confidence === "low" ? "warn" : "ok"}>{rate.confidence}</Badge>{" "}
                  {rate.blended_micros_per_million !== null
                    ? `${formatMicrosAsDollars(rate.blended_micros_per_million)} per 1M tokens (blended)`
                    : `${formatMicrosAsDollars(rate.input_micros_per_million)} in · ${formatMicrosAsDollars(rate.output_micros_per_million)} out`}
                  <span className="block text-faint">{rate.note}</span>
                </p>
              ))}
              {provider.skipped.length > 0 ? (
                <ul className="mt-1 list-disc pl-4 text-faint">
                  {provider.skipped.map((row) => (
                    <li key={row.model_key}>
                      <code className="font-mono">{row.model_key}</code> skipped — {row.reason}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))}
          {reconcileResult.skipped_providers.map((provider) => (
            <p key={provider.provider_id} className="text-faint">
              {provider.display_name} skipped — {provider.reason}
            </p>
          ))}
        </div>
      ) : null}
    </section>
  );
}
