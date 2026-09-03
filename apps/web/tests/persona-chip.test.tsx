/** The persona chip: on and off states, its accessible name, and where it links. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PersonaChip } from "@/components/personas/persona-chip";
import { personaSummary } from "./helpers/personas";

afterEach(cleanup);

describe("PersonaChip", () => {
  it("names the persona and links to its card in the library", () => {
    render(<PersonaChip persona={personaSummary()} />);
    const chip = screen.getByTestId("persona-chip");
    expect(chip.getAttribute("href")).toBe("/personas?persona=p1");
    expect(chip.getAttribute("data-state")).toBe("on");
    expect(chip.getAttribute("aria-label")).toBe("Persona Mission Control");
    expect(chip.getAttribute("title")).toBe("Persona: Mission Control");
    expect(chip.textContent).toContain("Mission Control");
    expect(chip.textContent).not.toContain("off");
    expect(screen.getByRole("link", { name: "Persona Mission Control" })).toBe(chip);
  });

  it("says when the persona is switched off in the library", () => {
    render(<PersonaChip persona={personaSummary({ enabled: false })} />);
    const chip = screen.getByTestId("persona-chip");
    expect(chip.getAttribute("data-state")).toBe("off");
    expect(chip.getAttribute("aria-label")).toBe("Persona Mission Control (switched off)");
    expect(chip.getAttribute("title")).toBe("Mission Control is switched off in the library");
    expect(screen.getByText("· off")).toBeTruthy();
    expect(chip.className).toContain("border-dashed");
  });
});
