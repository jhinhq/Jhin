"use client";

/** One short badge per card saying where an entry came from. The full
 * provenance sentence still travels on `title`, so the calm label costs
 * nobody the honest explanation — it just stops repeating it on every card. */

import { Badge } from "@/components/ui";
import { TRUST_COPY, TRUST_SHORT, TRUST_TONES } from "@/lib/apps";
import type { CatalogTrustTier } from "@/lib/types";

export function TrustBadge({
  tier,
  deprecated = false,
}: {
  tier: CatalogTrustTier;
  deprecated?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5" title={TRUST_COPY[tier]}>
      <Badge tone={TRUST_TONES[tier]}>{TRUST_SHORT[tier]}</Badge>
      {deprecated ? <Badge tone="warn">Deprecated</Badge> : null}
    </span>
  );
}
