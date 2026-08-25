/** People page: role copy, who may change whom, and the one-time invite link. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PeoplePage from "@/app/(app)/people/page";
import { api } from "@/lib/api";
import { useInvitations, useMembers } from "@/lib/hooks";
import type { Invitation, Member, WorkspaceRole } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/hooks", () => ({
  useMembers: vi.fn(),
  useInvitations: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ME = "u-me";

function member(overrides: Partial<Member> = {}): Member {
  return {
    id: `m-${overrides.user_id ?? "1"}`,
    user_id: "u-1",
    email: "ada@example.com",
    display_name: "Ada",
    role: "member",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function invitation(overrides: Partial<Invitation> = {}): Invitation {
  return {
    id: "i-1",
    email: "new@example.com",
    role: "member",
    status: "pending",
    invited_by_user_id: ME,
    invited_by_name: "Owner",
    expires_at: new Date(Date.now() + 6 * 86_400_000).toISOString(),
    accepted_at: null,
    revoked_at: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderPage(
  members: Member[],
  role: WorkspaceRole,
  invitations: Invitation[] = [],
) {
  vi.mocked(useMembers).mockReturnValue({
    data: members,
    isPending: false,
    refetch: () => undefined,
  } as unknown as ReturnType<typeof useMembers>);
  vi.mocked(useInvitations).mockReturnValue({
    data: invitations,
    isPending: false,
    refetch: () => undefined,
  } as unknown as ReturnType<typeof useInvitations>);

  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: ME,
          email: "me@example.com",
          display_name: "Me",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role,
        }}
      >
        <PeoplePage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("PeoplePage", () => {
  it("explains every role in plain language", () => {
    renderPage([member()], "owner");
    expect(
      screen.getByText(/Can set up apps, agents, automations, models, and budgets/),
    ).toBeDefined();
    expect(screen.getByText(/Cannot chat with agents or start work/)).toBeDefined();
  });

  it("shows members read-only to a member, with no role picker", () => {
    renderPage([member()], "member");
    expect(screen.getByText("ada@example.com")).toBeDefined();
    expect(screen.queryByRole("combobox", { name: /Role for Ada/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Invite someone/ })).toBeNull();
  });

  it("lets an admin change a member's role but never offers owner", () => {
    renderPage([member()], "admin");
    const select = screen.getByRole("combobox", { name: /Role for Ada/ }) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual([
      "viewer",
      "member",
      "admin",
    ]);
  });

  it("does not offer an admin any control over a peer admin", () => {
    renderPage(
      [member({ user_id: "u-2", display_name: "Grace", role: "admin" })],
      "admin",
    );
    expect(screen.queryByRole("combobox", { name: /Role for Grace/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Remove Grace/ })).toBeNull();
    // Their role is still shown, just as a badge instead of a control.
    expect(screen.getByTestId("member-list").textContent).toContain("Admin");
  });

  it("gives an owner full control over an admin", () => {
    renderPage(
      [member({ user_id: "u-2", display_name: "Grace", role: "admin" })],
      "owner",
    );
    const select = screen.getByRole("combobox", { name: /Role for Grace/ }) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toContain("owner");
    expect(screen.getByRole("button", { name: /Remove Grace/ })).toBeDefined();
  });

  it("reveals the invite link exactly once, with a copy-it-now warning", async () => {
    vi.mocked(api).mockResolvedValue({
      invitation: invitation(),
      invite_url: "http://localhost:3000/invite/tok-123",
      token: "tok-123",
    });
    renderPage([member()], "owner");

    fireEvent.click(screen.getByRole("button", { name: /Invite someone/ }));
    fireEvent.change(screen.getByRole("textbox", { name: /Email address/ }), {
      target: { value: "new@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create invite link/ }));

    await waitFor(() => {
      expect(screen.getByTestId("invite-link")).toBeDefined();
    });
    expect(screen.getByText("http://localhost:3000/invite/tok-123")).toBeDefined();
    expect(screen.getByText(/shown once, works once, and expires/)).toBeDefined();
    expect(vi.mocked(api).mock.calls[0][0]).toBe("/api/v1/workspaces/w1/invitations");
  });

  it("lists pending invitations with a revoke control, and hides accepted ones", () => {
    renderPage([member()], "owner", [
      invitation(),
      invitation({ id: "i-2", email: "done@example.com", status: "accepted" }),
    ]);

    expect(screen.getByText("new@example.com")).toBeDefined();
    expect(screen.queryByText("done@example.com")).toBeNull();
    expect(
      screen.getByRole("button", { name: /Revoke the invitation for new@example.com/ }),
    ).toBeDefined();
  });
});
