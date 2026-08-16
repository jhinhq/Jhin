import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "@/components/status-badge";

describe("StatusBadge", () => {
  it("renders the status text", () => {
    render(<StatusBadge status="ok" />);
    expect(screen.getByTestId("status-badge").textContent).toContain("ok");
  });

  it("falls back to error styling for unknown states", () => {
    render(<StatusBadge status="mystery" />);
    const badge = screen.getAllByTestId("status-badge").at(-1);
    expect(badge?.className).toContain("text-red-400");
  });
});
