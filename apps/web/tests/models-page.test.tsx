/** The recomposed Models page: decisions first (default model, model cards,
 * provider cards), machinery folded away — the pricing panel behind one
 * Advanced disclosure, and destructive actions behind the shared
 * ConfirmDialog instead of `window.confirm`. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ModelsPage from "@/app/(app)/models/page";
import type {
  ModelProfile,
  ModelProvider,
  PricingStatus,
  WorkspaceSpend,
} from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const PROVIDER: ModelProvider = {
  id: "prov-1",
  workspace_id: "workspace-1",
  type: "openai",
  display_name: "OpenAI",
  base_url: null,
  secret_id: "secret-1",
  credits_loaded_micros: null,
  has_admin_key: false,
  enabled: true,
  last_verified_at: null,
  last_error: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

const OLLAMA_PROVIDER: ModelProvider = {
  ...PROVIDER,
  id: "prov-ollama",
  type: "ollama",
  display_name: "Ollama Main",
  base_url: "http://192.168.1.79:11434/v1",
  secret_id: null,
};

const OLLAMA_MODELS = {
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

function profile(overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "profile-1",
    workspace_id: "workspace-1",
    provider_id: "prov-1",
    model_name: "gpt-5.6-terra",
    display_name: "Terra",
    context_window: 128_000,
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

const PROFILES: ModelProfile[] = [
  profile(),
  profile({
    id: "profile-2",
    display_name: "Nimbus",
    model_name: "gpt-5.6-nimbus",
    context_window: 32_000,
    input_cost_micros_per_million: null,
    output_cost_micros_per_million: null,
    price_source: null,
  }),
];

const WORKSPACE_DETAIL = {
  id: "workspace-1",
  name: "Acme",
  slug: "acme",
  status: "active",
  default_timezone: "UTC",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  default_model_profile_id: "profile-1",
};

const SPEND: WorkspaceSpend = {
  spent_month_micros: 12_500_000,
  spent_total_micros: 40_000_000,
  period_start: "2026-08-01T00:00:00Z",
  providers: [],
  monthly_budget_micros: null,
  warning_threshold: 0.8,
  fetched_at: "2026-08-28T00:00:00Z",
  untracked: [],
  untracked_runs: 0,
};

const PRICING: PricingStatus = {
  catalog_updated: "2026-01",
  catalog_stale: false,
  refreshed_source: null,
  refreshed_fetched_at: null,
  refreshed_entry_count: 0,
  refreshed_attribution: null,
  refreshed_project_url: "https://github.com/BerriAI/litellm",
  profiles: [],
  untracked: [],
  untracked_runs: 0,
  reconcile_available: false,
  reconcile_detail: "",
  pricing_pages: { openai: "https://platform.openai.com/docs/pricing" },
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Every request the page makes, with the writes kept for assertions. With
 * `ollama`, a local provider sits beside OpenAI and its host answers. */
function installServer(options: { ollama?: boolean } = {}) {
  const deletes: string[] = [];
  const providers = options.ollama ? [PROVIDER, OLLAMA_PROVIDER] : [PROVIDER];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (method === "DELETE" && path.startsWith("/api/v1/workspaces/workspace-1/model-profiles/")) {
        deletes.push(path);
        return new Response(null, { status: 204 });
      }
      if (method !== "GET") throw new Error(`Unexpected write: ${method} ${path}`);
      if (path === "/api/v1/workspaces/workspace-1/model-providers") return json(providers);
      if (path === "/api/v1/workspaces/workspace-1/model-providers/prov-ollama/ollama/models") {
        return json(OLLAMA_MODELS);
      }
      if (path === "/api/v1/workspaces/workspace-1/model-providers/prov-ollama/ollama/loaded") {
        return json({ models: [], detail: null, fetched_at: "2026-09-02T10:00:00Z" });
      }
      if (path === "/api/v1/workspaces/workspace-1/model-providers/prov-ollama/models") {
        return json({
          models: OLLAMA_MODELS.models.map((model) => ({
            id: model.name,
            input_cost_micros_per_million: null,
            output_cost_micros_per_million: null,
            context_window: null,
            source: null,
          })),
          detail: null,
          catalog_updated: "2026-01",
        });
      }
      if (path === "/api/v1/workspaces/workspace-1/model-profiles") return json(PROFILES);
      if (path === "/api/v1/workspaces/workspace-1") return json(WORKSPACE_DETAIL);
      if (path === "/api/v1/workspaces/workspace-1/spend") return json(SPEND);
      if (path === "/api/v1/workspaces/workspace-1/model-profiles/pricing-status") {
        return json(PRICING);
      }
      if (path === "/api/v1/workspaces/workspace-1/model-providers/prov-1/models") {
        return json({ models: [], detail: null, catalog_updated: "2026-01" });
      }
      if (path === "/api/v1/workspaces/workspace-1/secrets") return json([]);
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return deletes;
}

function renderPage(role: "owner" | "member" = "owner") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{ id: "user-1", email: "owner@example.com", display_name: "Owner", created_at: "2026-08-01T00:00:00Z" }}
        workspace={{ workspace_id: "workspace-1", workspace_name: "Acme", workspace_slug: "acme", role }}
      >
        <ModelsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("ModelsPage", () => {
  it("keeps the pricing machinery behind the Advanced disclosure", async () => {
    installServer();
    renderPage();
    expect(await screen.findByRole("heading", { name: "Default model" })).toBeDefined();

    // Folded by default: none of the machinery is on the page.
    expect(screen.queryByTestId("pricing-panel")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Where prices come from" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Advanced — where prices come from" }));
    const panel = await screen.findByTestId("pricing-panel");
    expect(within(panel).getByText("Where prices come from")).toBeDefined();
  });

  it("leads with the default model and one calm line per card", async () => {
    installServer();
    renderPage();
    expect(await screen.findByText("Choose the AI models your agents think with.")).toBeDefined();

    // The default hero and the profile card both carry the capability line
    // and the cost glyph rather than a pricing table.
    expect((await screen.findAllByText("Terra")).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Handles long documents/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\$\$/).length).toBeGreaterThan(0);
    expect(screen.getByText("Default")).toBeDefined();

    // The unpriced profile is flagged, never implied to be free.
    expect(screen.getByText("No price yet")).toBeDefined();

    // Only the non-default profile offers Make default.
    expect(screen.getAllByRole("button", { name: "Make default" })).toHaveLength(1);
  });

  it("deletes a profile through the shared ConfirmDialog, never window.confirm", async () => {
    const deletes = installServer();
    const confirmSpy = vi.spyOn(window, "confirm");
    renderPage();
    await screen.findByRole("heading", { name: "Default model" });

    // Edit the non-default profile (cards render in profile order).
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[1]);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    const dialog = await screen.findByTestId("confirm-dialog");
    expect(screen.getByRole("dialog", { name: /Delete profile .Nimbus.?/ })).toBeDefined();
    expect(deletes).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete profile" }));
    await waitFor(() => expect(deletes).toHaveLength(1));
    expect(deletes[0]).toBe("/api/v1/workspaces/workspace-1/model-profiles/profile-2");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("Use as model opens the profile dialog prefilled", async () => {
    installServer({ ollama: true });
    renderPage();
    await screen.findByRole("heading", { name: "Default model" });

    const card = await screen.findByTestId("provider-card-prov-ollama");
    fireEvent.click(within(card).getByRole("button", { name: "Manage" }));
    const manage = await screen.findByRole("dialog", { name: "Ollama Main" });
    const row = await within(manage).findByTestId("ollama-model-qwen3.8:latest");
    fireEvent.click(within(row).getByRole("button", { name: "Use as model" }));

    // The manage dialog gives way to a new-profile dialog that already knows
    // the model: name, identifier, the $0 price, and the host's context.
    const dialog = await screen.findByRole("dialog", { name: "New model profile" });
    expect(screen.queryByRole("dialog", { name: "Ollama Main" })).toBeNull();
    expect(within(dialog).getByDisplayValue("qwen3.8")).toBeDefined();
    expect(within(dialog).getByDisplayValue("qwen3.8:latest")).toBeDefined();
    expect((within(dialog).getByLabelText("Provider") as HTMLSelectElement).value).toBe(
      "prov-ollama",
    );
    expect(within(dialog).getByText("$0 in · $0 out per 1M tokens")).toBeDefined();
    expect(within(dialog).getByText("Runs on your Ollama host — no per-token price.")).toBeDefined();

    fireEvent.click(within(dialog).getByRole("button", { name: /Pricing \(auto-filled\)/ }));
    expect((within(dialog).getByLabelText("Input $ / 1M tokens") as HTMLInputElement).value).toBe("0");
    expect((within(dialog).getByLabelText("Output $ / 1M tokens") as HTMLInputElement).value).toBe("0");
    expect(
      (within(dialog).getByLabelText(/Context window \(tokens\)/) as HTMLInputElement).value,
    ).toBe("40960");
    // The provider's model list arriving later leaves the prefill alone.
    await waitFor(() => expect(within(dialog).getByText(/1 models available/)).toBeDefined());
    expect((within(dialog).getByLabelText("Input $ / 1M tokens") as HTMLInputElement).value).toBe("0");
  });

  it("shows viewers the decisions but none of the admin controls", async () => {
    installServer();
    renderPage("member");
    expect(await screen.findByRole("heading", { name: "Default model" })).toBeDefined();

    expect(screen.getAllByText("Terra").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Make default" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add provider/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /New profile/ })).toBeNull();
  });
});
