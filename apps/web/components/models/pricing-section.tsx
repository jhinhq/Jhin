"use client";

/** "Advanced — where prices come from": the pricing panel behind one closed
 * disclosure, with the two admin actions it offers (refresh the community
 * catalog, measure real rates from spend) owned here rather than by the
 * page, so the page stays a layout and this stays the one place the pricing
 * machinery is wired. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Disclosure } from "@/components/company/bits";
import { PricingPanel } from "@/components/pricing-panel";
import { api, errorText } from "@/lib/api";
import type { CatalogRefreshResult, PricingStatus, ReconcilePricingResult } from "@/lib/types";

export function PricingSection({
  workspaceId,
  isAdmin,
  status,
  isPending,
  onChanged,
}: {
  workspaceId: string;
  isAdmin: boolean;
  status: PricingStatus | undefined;
  isPending: boolean;
  /** Both actions can change stored prices; the page refetches everything. */
  onChanged: () => void;
}) {
  const [pricingError, setPricingError] = useState<string | null>(null);
  const [reconcileResult, setReconcileResult] = useState<ReconcilePricingResult | null>(null);
  const [catalogResult, setCatalogResult] = useState<CatalogRefreshResult | null>(null);

  const reconcile = useMutation({
    mutationFn: () =>
      api<ReconcilePricingResult>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/reconcile-pricing`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setPricingError(null);
      setReconcileResult(result);
      onChanged();
    },
    onError: (error) => setPricingError(errorText(error, "Measuring real rates failed.")),
  });

  const refreshCatalog = useMutation({
    mutationFn: () =>
      api<CatalogRefreshResult>(
        `/api/v1/workspaces/${workspaceId}/model-profiles/refresh-catalog`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setPricingError(null);
      setCatalogResult(result);
      onChanged();
    },
    onError: (error) =>
      setPricingError(errorText(error, "Refreshing the price catalog failed.")),
  });

  return (
    <Disclosure label="Advanced — where prices come from" openLabel="Hide where prices come from">
      <PricingPanel
        status={status}
        isPending={isPending}
        isAdmin={isAdmin}
        onReconcile={() => reconcile.mutate()}
        onRefreshCatalog={() => refreshCatalog.mutate()}
        reconciling={reconcile.isPending}
        refreshing={refreshCatalog.isPending}
        reconcileResult={reconcileResult}
        catalogResult={catalogResult}
        error={pricingError}
      />
    </Disclosure>
  );
}
