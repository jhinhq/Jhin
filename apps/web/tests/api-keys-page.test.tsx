/** API keys page: the scope tree in the create dialog, the one-time reveal,
 * the key list, and the usage log. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ApiKeysPage from "@/app/(app)/api-keys/page";
import { api } from "@/lib/api";
import { useApiKeys, useApiKeyUsage, useScopeCatalog } from "@/lib/hooks";
import type {
  ApiKeyInfo,
  ApiKeyUsageEntry,
  ScopeCatalog,
  WorkspaceRole,
} from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/hooks", () => ({
  useApiKeys: vi.fn(),
  useApiKeyUsage: vi.fn(),
  useScopeCatalog: vi.fn(),
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

const CATALOG: ScopeCatalog = {
  your_role: "member",
  categories: [
    {
      key: "chats",
      label: "Chats",
      description: "Conversations between people and agents.",
      scopes: [
        {
          key: "chats:read",
          category: "chats",
          action: "read",
          label: "Read chats",
          description: "Read conversations and their messages.",
          min_role: "viewer",
          available: true,
        },
      ],
    },
    {
      key: "audit",
      label: "Audit log",
      description: "The permanent record of who changed what.",
      scopes: [
        {
          key: "audit:read",
          category: "audit",
          action: "read",
          label: "Read the audit log",
          description: "Read the permanent record.",
          min_role: "admin",
          available: false,
        },
      ],
    },
  ],
};

function key(overrides: Partial<ApiKeyInfo> = {}): ApiKeyInfo {
  return {
    id: "k-1",
    name: "Nightly script",
    prefix: "abcd1234",
    scopes: ["chats:read"],
    role_ceiling: "member",
    created_by_user_id: ME,
    created_by_name: "Me",
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    created_at: "2026-01-01T00:00:00Z",
    status: "active",
    ...overrides,
  };
}

function usage(overrides: Partial<ApiKeyUsageEntry> = {}): ApiKeyUsageEntry {
  return {
    id: "u-1",
    api_key_id: "k-1",
    api_key_name: "Nightly script",
    api_key_prefix: "abcd1234",
    acting_user_id: ME,
    acting_user_name: "Me",
    method: "GET",
    path: "/api/v1/workspaces/{workspace_id}/agents",
    status_code: 200,
    created_at: "2026-01-02T09:00:00Z",
    ...overrides,
  };
}

function renderPage(
  keys: ApiKeyInfo[],
  role: WorkspaceRole = "member",
  usageItems: ApiKeyUsageEntry[] = [],
) {
  vi.mocked(useApiKeys).mockReturnValue({
    data: keys,
    isPending: false,
    refetch: () => undefined,
  } as unknown as ReturnType<typeof useApiKeys>);
  vi.mocked(useScopeCatalog).mockReturnValue({
    data: { ...CATALOG, your_role: role },
    isPending: false,
  } as unknown as ReturnType<typeof useScopeCatalog>);
  vi.mocked(useApiKeyUsage).mockReturnValue({
    data: { items: usageItems, total: usageItems.length },
    isPending: false,
  } as unknown as ReturnType<typeof useApiKeyUsage>);

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
        <ApiKeysPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("ApiKeysPage", () => {
  it("lists keys by prefix and scope, never by secret", () => {
    renderPage([key({ scopes: ["chats:read", "tasks:write"] })]);
    expect(screen.getByText("jhin_abcd1234_…")).toBeDefined();
    expect(screen.getByText("chats:read")).toBeDefined();
    expect(screen.getByText("tasks:write")).toBeDefined();
    expect(screen.getByTestId("api-key-list").textContent).toContain("never expires");
  });

  it("states the caller's ceiling so the cap is not a surprise", () => {
    renderPage([], "member");
    expect(screen.getByText(/A key can never do more than the person who created it/)).toBeDefined();
  });

  it("greys out scopes the caller's role cannot grant, with the reason", () => {
    renderPage([]);
    fireEvent.click(screen.getAllByRole("button", { name: /New key/ })[0]);
    fireEvent.click(screen.getByRole("button", { name: /Show Audit log permissions/ }));

    const toggle = screen.getByRole("checkbox", {
      name: /Read the audit log/,
    }) as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.getByText(/Needs the admin role/)).toBeDefined();
  });

  it("refuses to submit until a name and at least one permission are chosen", () => {
    renderPage([]);
    fireEvent.click(screen.getAllByRole("button", { name: /New key/ })[0]);

    const submit = screen.getByRole("button", { name: "Create key" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(screen.getByTestId("scope-count").textContent).toContain("at least one");

    fireEvent.change(screen.getByRole("textbox", { name: /What is this key for/ }), {
      target: { value: "Report script" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Chats/ }));
    expect((screen.getByRole("button", { name: "Create key" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(screen.getByTestId("scope-count").textContent).toContain("1 permission selected");
  });

  it("posts the chosen scopes and expiry, then reveals the key exactly once", async () => {
    vi.mocked(api).mockResolvedValue({ api_key: key(), key: "jhin_abcd1234_the-secret" });
    renderPage([]);
    fireEvent.click(screen.getAllByRole("button", { name: /New key/ })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: /What is this key for/ }), {
      target: { value: "Report script" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Chats/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "Expiry unit" }), {
      target: { value: "hours" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Expiry amount" }), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));

    await waitFor(() => expect(screen.getByTestId("api-key-reveal")).toBeDefined());
    expect(vi.mocked(api).mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: {
        name: "Report script",
        scopes: ["chats:read"],
        expires_unit: "hours",
        expires_in: 12,
      },
    });
    expect(screen.getByText("jhin_abcd1234_the-secret")).toBeDefined();
    expect(screen.getByText(/only time the key is shown/)).toBeDefined();
  });

  it("offers revoke on your own key and not on someone else's unless you are admin", () => {
    renderPage([key({ id: "k-2", name: "Theirs", created_by_user_id: "u-other" })], "member");
    expect(screen.queryByRole("button", { name: /Revoke Theirs/ })).toBeNull();

    cleanup();
    renderPage([key({ id: "k-2", name: "Theirs", created_by_user_id: "u-other" })], "admin");
    expect(screen.getByRole("button", { name: /Revoke Theirs/ })).toBeDefined();
  });

  it("does not offer revoke on an already revoked key", () => {
    renderPage([key({ status: "revoked", revoked_at: "2026-01-03T00:00:00Z" })]);
    expect(screen.queryByRole("button", { name: /Revoke Nightly script/ })).toBeNull();
    expect(screen.getByText("revoked")).toBeDefined();
  });

  it("shows the usage log with the route template and the outcome", () => {
    renderPage([key()], "member", [usage(), usage({ id: "u-2", status_code: 403 })]);
    const table = screen.getByTestId("usage-log");
    expect(table.textContent).toContain("/api/v1/workspaces/{workspace_id}/agents");
    expect(table.textContent).toContain("200");
    expect(table.textContent).toContain("403");
  });

  it("tells each role what slice of the usage log they are seeing", () => {
    renderPage([], "owner");
    expect(screen.getByText(/you see every API call made in this workspace/i)).toBeDefined();

    cleanup();
    renderPage([], "admin");
    expect(screen.getByText(/members and viewers/i)).toBeDefined();

    cleanup();
    renderPage([], "member");
    expect(screen.getByText(/calls made by your own keys/i)).toBeDefined();
  });
});
