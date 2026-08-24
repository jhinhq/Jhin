/** SkillsBrowseGallery: cards for skills found while browsing a known
 * source, with install state (docs/architecture/skills.md). */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SkillsBrowseGallery } from "@/components/skills-browse-gallery";
import type { BrowseSkillEntry } from "@/lib/types";

afterEach(cleanup);

function entry(overrides: Partial<BrowseSkillEntry> = {}): BrowseSkillEntry {
  return {
    source: "anthropics/skills",
    name: "pdf",
    description: "Work with PDF files.",
    path: "skills/pdf",
    installed: false,
    category: "Skills",
    ...overrides,
  };
}

describe("SkillsBrowseGallery", () => {
  it("renders an empty state when nothing matched", () => {
    render(
      <SkillsBrowseGallery
        entries={[]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath={null}
        onInstall={() => {}}
      />,
    );
    expect(screen.getByText("No skills found")).toBeTruthy();
  });

  it("renders a card per skill with name, description, and source badge", () => {
    render(
      <SkillsBrowseGallery
        entries={[entry(), entry({ name: "docx", path: "skills/docx" })]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath={null}
        onInstall={() => {}}
      />,
    );
    expect(screen.getByText("pdf")).toBeTruthy();
    expect(screen.getByText("docx")).toBeTruthy();
    expect(screen.getAllByText("Anthropic's official skills library").length).toBe(2);
  });

  it("fires onInstall with the entry when Install is clicked", () => {
    const onInstall = vi.fn();
    render(
      <SkillsBrowseGallery
        entries={[entry()]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath={null}
        onInstall={onInstall}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Install pdf" }));
    expect(onInstall).toHaveBeenCalledWith(entry());
  });

  it("shows an already-installed state and disables the button", () => {
    render(
      <SkillsBrowseGallery
        entries={[entry({ installed: true })]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath={null}
        onInstall={() => {}}
      />,
    );
    expect(screen.getAllByText("Installed").length).toBeGreaterThan(0);
    const button = screen.getByRole("button", { name: "Install pdf" });
    expect(button.hasAttribute("disabled")).toBe(true);
  });

  it("shows an installing state for the in-flight path only", () => {
    render(
      <SkillsBrowseGallery
        entries={[entry(), entry({ name: "docx", path: "skills/docx" })]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath="skills/pdf"
        onInstall={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Install pdf" }).textContent).toContain(
      "Installing",
    );
    expect(screen.getByRole("button", { name: "Install docx" }).textContent).not.toContain(
      "Installing",
    );
  });

  it("groups cards into sections by category", () => {
    render(
      <SkillsBrowseGallery
        entries={[
          entry({ name: "pdf", category: "Document skills" }),
          entry({ name: "docx", path: "skills/docx", category: "Document skills" }),
          entry({ name: "brainstorming", path: "skills/brainstorming", category: "Skills" }),
        ]}
        sourceLabel="Anthropic's official skills library"
        canInstall
        installingPath={null}
        onInstall={() => {}}
      />,
    );
    expect(screen.getByTestId("browse-category-Document skills")).toBeTruthy();
    expect(screen.getByTestId("browse-category-Skills")).toBeTruthy();
  });

  it("hides the install button for non-admins", () => {
    render(
      <SkillsBrowseGallery
        entries={[entry()]}
        sourceLabel="Anthropic's official skills library"
        canInstall={false}
        installingPath={null}
        onInstall={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Install pdf" })).toBeNull();
  });
});
