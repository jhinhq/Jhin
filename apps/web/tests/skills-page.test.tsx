/** Skills library page: rows with badges, the review banner, and the
 * admin actions (install starters, enable/disable). */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SkillsPage from "@/app/(app)/skills/page";
import { api } from "@/lib/api";
import { useSkills } from "@/lib/hooks";
import type { Skill } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/hooks", () => ({
  useSkills: vi.fn(),
  useSkill: vi.fn(() => ({ data: undefined, isPending: false })),
  useInvalidateSkills: () => () => undefined,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn(), apiUpload: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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
