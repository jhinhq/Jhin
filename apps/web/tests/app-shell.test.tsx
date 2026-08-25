/** Shell nav: primary group, collapsible Advanced group, attention badge. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SidebarNav } from "@/components/app-shell";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/hooks", () => ({
  useMe: () => ({ data: undefined, isPending: false, error: null }),
  useBootstrapStatus: () => ({ data: { needs_bootstrap: false } }),
  useAttention: () => ({
    data: {
      pending_approvals: [],
      failed_tasks: [],
      waiting_conversations: [],
      counts: { approvals: 2, failures: 1, total: 3 },
    },
  }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/runs",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderNav(pathname = "/chats") {
  return render(
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
        role: "owner",
      }}
    >
      <SidebarNav pathname={pathname} />
    </WorkspaceProvider>,
  );
}

describe("SidebarNav", () => {
  it("renders the primary group with the attention badge and marks the current page", () => {
    renderNav("/chats");
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const label of [
      "Home",
      "Chats",
      "Agents",
      "Company",
      "Activity",
      "Attention",
      "Automations",
      "Apps",
    ]) {
      expect(within(nav).getByRole("link", { name: new RegExp(label) })).toBeTruthy();
    }
    // Home leads the primary group.
    const primary = within(nav).getAllByRole("link").slice(0, 2).map((link) => link.textContent);
    expect(primary).toEqual(["Home", "Chats"]);
    expect(within(nav).getByRole("link", { name: /Chats/ }).getAttribute("aria-current")).toBe(
      "page",
    );
    expect(screen.getByTestId("nav-badge").textContent).toBe("3");
  });

  it("stays collapsed on a primary page until opened", () => {
    renderNav("/chats");
    const toggle = screen.getByRole("button", { name: /Advanced/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("link", { name: "Work queue" })).toBeNull();
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(localStorage.getItem("jhin-advanced-open")).toBe("true");
  });

  it("opens automatically on an Advanced page and remembers the choice", () => {
    renderNav("/runs");
    const toggle = screen.getByRole("button", { name: /Advanced/ });
    // Landing on an Advanced route reveals the group so the active item is visible.
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(localStorage.getItem("jhin-advanced-open")).toBe("true");
    for (const label of [
      "Work queue",
      "Runs",
      "Approvals",
      "Triggers",
      "Models",
      "Audit",
      "Settings",
    ]) {
      expect(screen.getByRole("link", { name: label })).toBeTruthy();
    }
    // Connectors merged into Apps; the redundant index link is gone.
    expect(screen.queryByRole("link", { name: "Connectors" })).toBeNull();
    expect(screen.queryByRole("link", { name: "All advanced tools" })).toBeNull();
    expect(screen.getByRole("link", { name: "Runs" }).getAttribute("aria-current")).toBe("page");

    fireEvent.click(toggle);
    expect(localStorage.getItem("jhin-advanced-open")).toBe("false");
    expect(screen.queryByRole("link", { name: "Work queue" })).toBeNull();
  });

  it("starts open when localStorage says so", () => {
    localStorage.setItem("jhin-advanced-open", "true");
    renderNav("/tasks");
    expect(screen.getByRole("link", { name: "Work queue" }).getAttribute("aria-current")).toBe(
      "page",
    );
  });
});
