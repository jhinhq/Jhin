/** TrustBadge: one short provenance word per card, with the full sentence
 * riding on `title` so the calm label never costs the honest explanation. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TrustBadge } from "@/components/catalog/trust-badge";
import { trustLabel } from "@/lib/apps";
import type { CatalogTrustTier } from "@/lib/types";

afterEach(cleanup);

const SHORT_LABELS: [CatalogTrustTier, string][] = [
  ["curated", "Curated"],
  ["registry_verified", "Verified"],
  ["smithery_verified", "Community"],
  ["reviewed", "Reviewed"],
  ["indexed", "Community"],
];

describe("TrustBadge", () => {
  it("shows the short label with the full provenance sentence on title", () => {
    for (const [tier, short] of SHORT_LABELS) {
      const { unmount } = render(<TrustBadge tier={tier} />);
      const badge = screen.getByText(short);
      expect(badge.closest("[title]")?.getAttribute("title")).toBe(trustLabel(tier));
      unmount();
    }
  });

  it("colours reassurance, neutrality, and caution apart", () => {
    render(<TrustBadge tier="reviewed" />);
    expect(screen.getByText("Reviewed").className).toContain("text-ok");
    cleanup();
    render(<TrustBadge tier="indexed" />);
    expect(screen.getByText("Community").className).toContain("text-warn");
    cleanup();
    render(<TrustBadge tier="smithery_verified" />);
    expect(screen.getByText("Community").className).toContain("text-dim");
  });

  it("adds a Deprecated badge only when asked", () => {
    render(<TrustBadge tier="registry_verified" deprecated />);
    expect(screen.getByText("Verified")).toBeDefined();
    expect(screen.getByText("Deprecated").className).toContain("text-warn");
    cleanup();
    render(<TrustBadge tier="registry_verified" />);
    expect(screen.queryByText("Deprecated")).toBeNull();
  });
});
