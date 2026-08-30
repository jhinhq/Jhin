/** Skills library page: rows with badges, the review banner, and the
 * admin actions (install starters, enable/disable). */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillsPage from "@/app/(app)/skills/page";
import { api } from "@/lib/api";
import { useBrowseSkills, useSkills, useSkillSources } from "@/lib/hooks";
import type { BrowseSkillEntry, Skill, SkillSourceInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

// The catalog-backed gallery has its own suite (skill-catalog-gallery.test.tsx);
// here it is stubbed so this page test stays about the page — it echoes the
// role the page hands down and, when a test flips `mockCatalogEmpty`, reports
// an empty catalog the way the real gallery does after its query resolves.
let mockCatalogEmpty = false;
vi.mock("@/components/skill-catalog-gallery", () => ({
  SkillCatalogGallery: ({
    isAdmin,
    onCatalogEmpty,
  }: {
    workspaceId: string;
    isAdmin: boolean;
    onCatalogEmpty?: (empty: boolean) => void;
  }) => {
    useEffect(() => {
      onCatalogEmpty?.(mockCatalogEmpty);
    }, [onCatalogEmpty]);
    return <div data-testid="skill-catalog-gallery">{isAdmin ? "can install" : "read only"}</div>;
  },
}));

vi.mock("@/lib/hooks", () => ({
  useSkills: vi.fn(),
  useSkill: vi.fn(() => ({ data: undefined, isPending: false })),
  useInvalidateSkills: () => () => undefined,
  useInvalidateSkillSources: () => () => undefined,
  useSkillSources: vi.fn(() => ({ data: [] as SkillSourceInfo[], isError: false })),
  useBrowseSkills: vi.fn(() => ({
    data: undefined,
    isPending: false,
    isError: false,
    error: null,
    refetch: () => undefined,
  })),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn(), apiUpload: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mockCatalogEmpty = false;
});

/** The direct GitHub browser sits behind an Advanced disclosure on the
 * "Find new skills" tab now; the browse tests reach it by opening both. */
function openBrowseTab() {
  fireEvent.click(screen.getByRole("tab", { name: "Find new skills" }));
  fireEvent.click(
    screen.getByRole("button", { name: "Advanced — browse a GitHub library directly" }),
  );
}

function skill(overrides: Partial<Skill> = {}): Skill {
  return {
    id: "s1",
    workspace_id: "w1",
    name: "release-notes",
    description: "Write user-facing release notes.",
    source: "built_in",
    source_url: "",
    enabled: true,
    version: 1,
    file_count: 1,
    category: "General",
    created_by_agent_id: null,
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
    ...overrides,
  };
}

function renderPage(items: Skill[], role: "owner" | "member" = "owner") {
  vi.mocked(useSkills).mockReturnValue({
    data: { items, total: items.length },
    isPending: false,
    isError: false,
  } as ReturnType<typeof useSkills>);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u1",
          email: "ada@example.com",
          display_name: "Ada",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role,
        }}
      >
        <SkillsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("SkillsPage", () => {
  it("lists skills with source badges and file counts", () => {
    renderPage([
      skill(),
      skill({ id: "s2", name: "custom-thing", source: "custom", file_count: 0 }),
    ]);
    expect(screen.getByText("release-notes")).toBeTruthy();
    expect(screen.getByText("Starter")).toBeTruthy();
    expect(screen.getByText("custom-thing")).toBeTruthy();
    expect(screen.getByText("Custom")).toBeTruthy();
    expect(screen.getByText("1 file")).toBeTruthy();
  });

  it("shows the review banner for disabled imports", () => {
    renderPage([skill({ id: "s3", name: "from-repo", source: "imported", enabled: false })]);
    expect(screen.getByTestId("review-banner").textContent).toContain("waiting for review");
    expect(screen.getByText("Review and enable")).toBeTruthy();
  });

  it("installs the starter skills", async () => {
    vi.mocked(api).mockResolvedValue({ installed: 5, skipped: 0, names: [] });
    renderPage([]);
    fireEvent.click(screen.getAllByRole("button", { name: /Install starter skills/ })[0]);
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/skills/install-builtins", {
        method: "POST",
      }),
    );
  });

  it("toggles a skill's enabled state", async () => {
    vi.mocked(api).mockResolvedValue(skill({ enabled: false }));
    renderPage([skill()]);
    fireEvent.click(screen.getByRole("button", { name: "Disable release-notes" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/skills/s1", {
        method: "PATCH",
        body: { enabled: false },
      }),
    );
  });

  it("hides admin actions from members", () => {
    renderPage([skill()], "member");
    expect(screen.queryByRole("button", { name: /Install starter skills/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Disable release-notes" })).toBeNull();
  });
});

function browseEntry(overrides: Partial<BrowseSkillEntry> = {}): BrowseSkillEntry {
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

function sourceInfo(overrides: Partial<SkillSourceInfo> = {}): SkillSourceInfo {
  return {
    source: "anthropics/skills",
    label: "Anthropic's official skills library",
    description: "",
    url: "https://github.com/anthropics/skills",
    custom: false,
    ...overrides,
  };
}

describe("SkillsPage — Find new skills", () => {
  it("leads with the reviewed gallery and keeps the GitHub browser behind Advanced", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()]);
    fireEvent.click(screen.getByRole("tab", { name: "Find new skills" }));

    expect(screen.getByTestId("skill-catalog-gallery").textContent).toBe("can install");
    // The direct GitHub browse stays folded until somebody asks for it.
    expect(screen.queryByLabelText("Search skills")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Advanced — browse a GitHub library directly" }),
    );
    expect(screen.getByLabelText("Search skills")).toBeTruthy();
    expect(screen.getByText("pdf")).toBeTruthy();
  });

  it("opens the Advanced GitHub browser itself when the gallery is empty", () => {
    mockCatalogEmpty = true;
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()]);
    fireEvent.click(screen.getByRole("tab", { name: "Find new skills" }));

    // No extra click: the browse section is already breathing.
    expect(screen.getByLabelText("Search skills")).toBeTruthy();
    expect(screen.getByText("pdf")).toBeTruthy();
  });

  it("tells the gallery when the viewer cannot install", () => {
    renderPage([skill()], "member");
    fireEvent.click(screen.getByRole("tab", { name: "Find new skills" }));
    expect(screen.getByTestId("skill-catalog-gallery").textContent).toBe("read only");
  });

  it("shows a search box and results after switching tabs", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry(), browseEntry({ name: "docx" })] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()]);
    openBrowseTab();
    expect(screen.getByLabelText("Search skills")).toBeTruthy();
    expect(screen.getByText("pdf")).toBeTruthy();
    expect(screen.getByText("docx")).toBeTruthy();
  });

  it("installs a browsed skill", async () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    vi.mocked(api).mockResolvedValue({ skill: skill({ name: "pdf" }), status: "installed" });
    renderPage([skill()]);
    openBrowseTab();
    fireEvent.click(screen.getByRole("button", { name: "Install pdf" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/skills/browse/install", {
        method: "POST",
        body: { source: "anthropics/skills", skill_path: "skills/pdf" },
      }),
    );
  });

  it("shows a friendly error when GitHub is unreachable", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("could not reach GitHub"),
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()]);
    openBrowseTab();
    expect(screen.queryByText(/No skills found/)).toBeNull();
    // An ErrorNote renders — the page does not crash and shows some message.
    expect(document.body.textContent).toMatch(/could not reach|GitHub/i);
  });

  it("hides the install action from non-admins", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()], "member");
    openBrowseTab();
    expect(screen.queryByRole("button", { name: "Install pdf" })).toBeNull();
  });

  it("shows an Add a source button for admins and adds one live-validated source", async () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    vi.mocked(api).mockResolvedValue(sourceInfo({ source: "obra/superpowers", custom: true }));
    renderPage([skill()]);
    openBrowseTab();
    fireEvent.click(screen.getByRole("button", { name: "Add a source" }));
    fireEvent.change(screen.getByPlaceholderText("owner/repo or owner/repo/path"), {
      target: { value: "obra/superpowers" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/skill-sources", {
        method: "POST",
        body: { source: "obra/superpowers", label: "" },
      }),
    );
  });

  it("hides Add a source from non-admins", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: { source: "anthropics/skills", skills: [browseEntry()] },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()], "member");
    openBrowseTab();
    expect(screen.queryByRole("button", { name: "Add a source" })).toBeNull();
  });

  it("groups browse results into category sections", () => {
    vi.mocked(useSkillSources).mockReturnValue({
      data: [sourceInfo()],
      isError: false,
    } as ReturnType<typeof useSkillSources>);
    vi.mocked(useBrowseSkills).mockReturnValue({
      data: {
        source: "anthropics/skills",
        skills: [browseEntry(), browseEntry({ name: "template-skill", category: "General" })],
      },
      isPending: false,
      isError: false,
      error: null,
      refetch: () => undefined,
    } as unknown as ReturnType<typeof useBrowseSkills>);
    renderPage([skill()]);
    openBrowseTab();
    expect(screen.getByTestId("browse-category-Skills")).toBeTruthy();
    expect(screen.getByTestId("browse-category-General")).toBeTruthy();
  });
});

describe("SkillsPage — category grouping and filter", () => {
  it("groups the library into collapsible category sections", () => {
    renderPage([
      skill({ id: "s1", name: "release-notes", category: "Engineering" }),
      skill({ id: "s2", name: "meeting-notes", category: "Communication" }),
      skill({ id: "s3", name: "misc-thing", category: "General" }),
    ]);
    expect(screen.getByTestId("skill-category-Engineering")).toBeTruthy();
    expect(screen.getByTestId("skill-category-Communication")).toBeTruthy();
    expect(screen.getByTestId("skill-category-General")).toBeTruthy();
  });

  it("filters the library to one category via the chip row", () => {
    renderPage([
      skill({ id: "s1", name: "release-notes", category: "Engineering" }),
      skill({ id: "s2", name: "meeting-notes", category: "Communication" }),
    ]);
    expect(screen.getByText("release-notes")).toBeTruthy();
    expect(screen.getByText("meeting-notes")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Communication" }));
    expect(screen.queryByText("release-notes")).toBeNull();
    expect(screen.getByText("meeting-notes")).toBeTruthy();
    // Clicking the active chip again clears the filter.
    fireEvent.click(screen.getByRole("button", { name: "Communication" }));
    expect(screen.getByText("release-notes")).toBeTruthy();
  });
});
