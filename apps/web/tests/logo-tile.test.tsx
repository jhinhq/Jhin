/** The library's lead tile and its four-rung fallback chain: proxied logo →
 * named Lucide glyph → monogram → plug. The chain is what makes logos safe to
 * want — a broken proxy or an air-gapped install lands on exactly the tile
 * the library rendered before logos existed. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { LogoTile } from "@/components/catalog/logo-tile";

afterEach(cleanup);

describe("LogoTile", () => {
  it("shows the proxied logo when one is given", () => {
    const { container } = render(
      <LogoTile name="Sentry" icon="bug" logoUrl="/api/v1/catalog/entries/sentry/icon" />,
    );
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("/api/v1/catalog/entries/sentry/icon");
    expect(img?.getAttribute("loading")).toBe("lazy");
    // Decorative: an empty alt and aria-hidden keep it out of the tree.
    expect(img?.getAttribute("alt")).toBe("");
    expect(img?.getAttribute("aria-hidden")).toBe("true");
  });

  it("falls back to the named glyph when the logo fails to load", () => {
    const { container } = render(
      <LogoTile name="Sentry" icon="bug" logoUrl="/api/v1/catalog/entries/sentry/icon" />,
    );
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("falls back to a monogram when the icon name is unknown", () => {
    const { container } = render(<LogoTile name="kestrel" icon="no-such-glyph" />);
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText("K")).toBeDefined();
  });

  it("monograms when no icon is given at all", () => {
    const { container } = render(<LogoTile name="acme" />);
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByText("A")).toBeDefined();
  });

  it("ends the chain at the plug for a nameless entry", () => {
    const { container } = render(<LogoTile name="  " />);
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("sizes the tile to the three allowed steps, defaulting to 40", () => {
    const { container, rerender } = render(<LogoTile name="X" size={36} />);
    expect(container.firstElementChild?.className).toContain("h-9 w-9");
    rerender(<LogoTile name="X" />);
    expect(container.firstElementChild?.className).toContain("h-10 w-10");
    rerender(<LogoTile name="X" size={48} />);
    expect(container.firstElementChild?.className).toContain("h-12 w-12");
  });
});
