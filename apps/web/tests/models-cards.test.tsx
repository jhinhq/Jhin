/** Render tests for the recomposed Models page pieces: the default-model
 * hero card, the profile cards, the simplified provider cards, and the
 * manage dialog — including that destructive actions go through the shared
 * ConfirmDialog rather than window.confirm. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChangeDefaultDialog } from "@/components/models/change-default-dialog";
import { DefaultModelCard } from "@/components/models/default-model-card";
import { ProfileCard } from "@/components/models/profile-card";
import { ProviderCard } from "@/components/models/provider-card";
import { ProviderManageDialog } from "@/components/models/provider-manage-dialog";
import { api } from "@/lib/api";
import type { ModelProfile, ModelProvider, ProviderBalance } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function profile(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "p1",
    workspace_id: "w1",
    provider_id: "prov-1",
    model_name: "gpt-5-mini",
    display_name: "GPT-5 mini",
    context_window: 200_000,
    input_cost_micros_per_million: 2_500_000,
    output_cost_micros_per_million: 10_000_000,
    price_source: "catalog",
    supports_tools: true,
    supports_reasoning: false,
    config_json: {},
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function provider(overrides: Partial<ModelProvider> = {}): ModelProvider {
  return {
    id: "prov-1",
    workspace_id: "w1",
    type: "openai",
    display_name: "OpenAI",
    base_url: null,
    secret_id: "sec-1",
    credits_loaded_micros: null,
    has_admin_key: false,
    enabled: true,
    last_verified_at: "2026-08-20T00:00:00Z",
    last_error: null,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function balance(overrides: Partial<ProviderBalance> = {}): ProviderBalance {
  return {
    tracked_spent_month_micros: 1_000_000,
    tracked_spent_total_micros: 5_000_000,
    provider_spent_month_micros: null,
    provider_remaining_micros: null,
    credits_loaded_micros: null,
    estimated_remaining_micros: null,
    source: "tracked",
    detail: "Tracked by Jhin",
    fetched_at: "2026-08-28T00:00:00Z",
    ...overrides,
  };
}

function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe("DefaultModelCard", () => {
  it("renders the profile name, the capability line, and the tier glyph", () => {
    renderWithQuery(
      <DefaultModelCard
        profile={profile()}
        provider={provider()}
        isAdmin
        onChange={() => undefined}
      />,
    );
    expect(screen.getByText("GPT-5 mini")).toBeTruthy();
    expect(screen.getByText("Handles long documents")).toBeTruthy();
    // $12.50 combined per 1M tokens → tier 2 → "$$ Moderate".
    expect(screen.getByText("$$")).toBeTruthy();
    expect(screen.getByText(/Moderate/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Change" })).toBeTruthy();
  });

  it("keeps the exact price pair one hover away on the cost line", () => {
    renderWithQuery(
      <DefaultModelCard
        profile={profile()}
        provider={provider()}
        isAdmin
        onChange={() => undefined}
      />,
    );
    expect(screen.getByTitle("$2.50 in · $10.00 out per 1M tokens")).toBeTruthy();
  });

  it("shows the empty state when no default is set", () => {
    renderWithQuery(
      <DefaultModelCard profile={null} provider={null} isAdmin onChange={() => undefined} />,
    );
    expect(screen.getByText("No default model yet")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
  });

  it("hides the Change button from viewers", () => {
    renderWithQuery(
      <DefaultModelCard
        profile={profile()}
        provider={provider()}
        isAdmin={false}
        onChange={() => undefined}
      />,
    );
    expect(screen.getByText("GPT-5 mini")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
  });
});

describe("ProfileCard", () => {
  const cardProps = {
    provider: provider(),
    workspaceId: "w1",
    pricing: undefined,
    pricingPages: undefined,
    onChanged: () => undefined,
    onError: () => undefined,
    onEdit: () => undefined,
  };

  it("shows the Default badge and hides Make default on the default profile", () => {
    renderWithQuery(<ProfileCard {...cardProps} profile={profile()} isDefault isAdmin />);
    expect(screen.getByText("Default")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Make default" })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("offers Make default on a non-default profile", () => {
    renderWithQuery(<ProfileCard {...cardProps} profile={profile()} isDefault={false} isAdmin />);
    expect(screen.queryByText("Default")).toBeNull();
    expect(screen.getByRole("button", { name: "Make default" })).toBeTruthy();
  });

  it("marks an unpriced profile instead of implying it is free", () => {
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        profile={profile({
          input_cost_micros_per_million: null,
          output_cost_micros_per_million: null,
          price_source: null,
        })}
        isDefault={false}
        isAdmin
      />,
    );
    expect(screen.getByText("No price yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add price" })).toBeTruthy();
    expect(screen.queryByTestId("profile-cost-line")).toBeNull();
  });

  it("shows viewers the facts but none of the admin buttons", () => {
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        profile={profile({
          input_cost_micros_per_million: null,
          output_cost_micros_per_million: null,
        })}
        isDefault={false}
        isAdmin={false}
      />,
    );
    expect(screen.getByText("GPT-5 mini")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Make default" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add price" })).toBeNull();
  });

  it("keeps the raw model identifier off the card face but on the name's title", () => {
    renderWithQuery(<ProfileCard {...cardProps} profile={profile()} isDefault={false} isAdmin />);
    expect(screen.getByTitle("gpt-5-mini")).toBeTruthy();
  });
});

describe("ProviderCard", () => {
  it("shows status as words, the model count, and a Manage button", () => {
    const onManage = vi.fn();
    renderWithQuery(
      <ProviderCard provider={provider()} typeLabel="OpenAI" profileCount={2} onManage={onManage} />,
    );
    expect(screen.getByText("Connected")).toBeTruthy();
    expect(screen.getByText("2 models")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    expect(onManage).toHaveBeenCalled();
  });

  it("flags a provider with a stored error", () => {
    renderWithQuery(
      <ProviderCard
        provider={provider({ last_error: "401 from the provider" })}
        typeLabel="OpenAI"
        profileCount={1}
        onManage={() => undefined}
      />,
    );
    expect(screen.getByText("Needs attention")).toBeTruthy();
    expect(screen.getByText("1 model")).toBeTruthy();
  });

  it("labels a disabled provider as turned off", () => {
    renderWithQuery(
      <ProviderCard
        provider={provider({ enabled: false })}
        typeLabel="OpenAI"
        profileCount={0}
        onManage={() => undefined}
      />,
    );
    expect(screen.getByText("Turned off")).toBeTruthy();
  });
});

describe("ProviderManageDialog", () => {
  function renderDialog(overrides: { isAdmin?: boolean; provider?: ModelProvider } = {}) {
    vi.mocked(api).mockImplementation(async (path: string, options?: { method?: string }) => {
      if (path.endsWith("/balance")) return balance();
      if (path.endsWith("/ollama/models")) {
        return {
          models: [
            {
              name: "qwen3.8:latest",
              size_bytes: 17_700_000_000,
              family: "qwen3",
              parameter_size: "27.3B",
              quantization: "Q4_K_M",
              modified_at: "2026-08-30T12:00:00Z",
              context_length: 40_960,
              capabilities: ["completion", "tools", "thinking"],
              loaded: false,
              size_vram_bytes: null,
              expires_at: null,
              keeps_loaded: false,
            },
          ],
          detail: null,
          fetched_at: "2026-09-02T10:00:00Z",
        };
      }
      if (path.endsWith("/ollama/loaded")) {
        return { models: [], detail: null, fetched_at: "2026-09-02T10:00:00Z" };
      }
      if (options?.method === "DELETE") return undefined;
      throw new Error(`Unexpected request: ${options?.method ?? "GET"} ${path}`);
    });
    const onChanged = vi.fn();
    const onClose = vi.fn();
    const onUseAsModel = vi.fn();
    renderWithQuery(
      <ProviderManageDialog
        workspaceId="w1"
        provider={overrides.provider ?? provider()}
        typeLabel="OpenAI"
        profileCount={2}
        isDefaultProvider
        isAdmin={overrides.isAdmin ?? true}
        onClose={onClose}
        onChanged={onChanged}
        onEdit={() => undefined}
        onAddAdminKey={() => undefined}
        onUseAsModel={onUseAsModel}
      />,
    );
    return { onChanged, onClose, onUseAsModel };
  }

  it("shows the local-models panel instead of the balance block for an Ollama provider", async () => {
    const { onUseAsModel } = renderDialog({
      provider: provider({
        type: "ollama",
        display_name: "Ollama Main",
        secret_id: null,
        base_url: "http://192.168.1.79:11434/v1",
      }),
    });
    const panel = await screen.findByTestId("ollama-panel");
    expect(screen.queryByTestId("balance-block")).toBeNull();
    expect(within(panel).getByText("Local models")).toBeTruthy();
    expect(await within(panel).findByTestId("ollama-model-qwen3.8:latest")).toBeTruthy();
    for (const call of vi.mocked(api).mock.calls) {
      expect(String(call[0])).not.toContain("/balance");
    }

    fireEvent.click(within(panel).getByRole("button", { name: "Use as model" }));
    expect(onUseAsModel).toHaveBeenCalledWith({
      providerId: "prov-1",
      modelName: "qwen3.8:latest",
      displayName: "qwen3.8",
      contextWindow: 40_960,
      inputCostMicros: 0,
      outputCostMicros: 0,
    });
  });

  it("deletes through the shared ConfirmDialog, never window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    const { onClose } = renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByTestId("confirm-dialog");
    expect(confirm.textContent).toContain("This also removes its 2 model profiles");
    expect(confirm.textContent).toContain("clears the workspace default");

    fireEvent.click(within(confirm).getByRole("button", { name: "Delete provider" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/model-providers/prov-1", {
        method: "DELETE",
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("hides the admin actions from viewers but keeps the facts", async () => {
    renderDialog({ isAdmin: false });
    expect(await screen.findByTestId("balance-block")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Verify/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });
});

describe("ChangeDefaultDialog", () => {
  it("saves the picked profile as the workspace default", async () => {
    vi.mocked(api).mockResolvedValue({});
    const onChanged = vi.fn();
    renderWithQuery(
      <ChangeDefaultDialog
        workspaceId="w1"
        profiles={[profile(), profile({ id: "p2", display_name: "Claude", provider_id: "prov-1" })]}
        providers={[provider()]}
        currentDefaultId="p1"
        onClose={() => undefined}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByTestId("default-option-p2"));
    fireEvent.click(screen.getByRole("button", { name: "Make it the default" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1", {
        method: "PATCH",
        body: { default_model_profile_id: "p2" },
      }),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("disables confirming the profile that is already the default", () => {
    renderWithQuery(
      <ChangeDefaultDialog
        workspaceId="w1"
        profiles={[profile()]}
        providers={[provider()]}
        currentDefaultId="p1"
        onClose={() => undefined}
        onChanged={() => undefined}
      />,
    );
    const confirm = screen.getByRole("button", { name: "Make it the default" });
    expect(confirm.hasAttribute("disabled")).toBe(true);
  });
});
