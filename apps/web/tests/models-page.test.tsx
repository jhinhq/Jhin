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
    assumed_free: false,
  }),
];

/** A local model turned into a profile and left unpriced: the API reports
 * it as assumed free rather than as a gap, and stores nothing. */
const ASSUMED_FREE_PROFILE: ModelProfile = profile({
  id: "profile-ollama",
  provider_id: "prov-ollama",
  model_name: "qwen3.8:latest",
  display_name: "qwen3.8",
  context_window: 40_960,
  input_cost_micros_per_million: null,
  output_cost_micros_per_million: null,
  price_source: null,
  assumed_free: true,
});

const ASSUMED_FREE_LABEL =
  "Assumed free: a self-hosted endpoint has no per-token price. Enter prices if this endpoint bills you.";

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
 * `ollama`, a local provider sits beside OpenAI and its host answers;
 * `profiles`, `pricing` and `defaultProfileId` replace the default rows. */
function installServer(
  options: {
    ollama?: boolean;
    profiles?: ModelProfile[];
    pricing?: PricingStatus;
    defaultProfileId?: string;
  } = {},
) {
  const deletes: string[] = [];
  const providers = options.ollama ? [PROVIDER, OLLAMA_PROVIDER] : [PROVIDER];
  const profiles = options.profiles ?? PROFILES;
  const pricing = options.pricing ?? PRICING;
  const workspaceDetail = {
    ...WORKSPACE_DETAIL,
    default_model_profile_id: options.defaultProfileId ?? WORKSPACE_DETAIL.default_model_profile_id,
  };
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
      if (path === "/api/v1/workspaces/workspace-1/model-profiles") return json(profiles);
      if (path === "/api/v1/workspaces/workspace-1") return json(workspaceDetail);
      if (path === "/api/v1/workspaces/workspace-1/spend") return json(SPEND);
      if (path === "/api/v1/workspaces/workspace-1/model-profiles/pricing-status") {
        return json(pricing);
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

  it("puts the Ollama card first with its panel open and a header line saying what is loaded", async () => {
    installServer({ ollama: true });
    renderPage();
    await screen.findByRole("heading", { name: "Providers" });

    // The card with live state leads the grid, and is the wide one.
    const cards = screen.getAllByTestId(/^provider-card-/);
    expect(cards.map((card) => card.getAttribute("data-testid"))).toEqual([
      "provider-card-prov-ollama",
      "provider-card-prov-1",
    ]);
    const [ollama, cloud] = cards;
    expect(ollama.className).toContain("md:col-span-2");

    // The panel is simply on the card — nothing to open — and the header
    // line answers before anyone scrolls to it.
    expect(within(ollama).getByTestId("ollama-panel")).toBeDefined();
    expect(await within(ollama).findByTestId("ollama-model-qwen3.8:latest")).toBeDefined();
    await waitFor(() =>
      expect(within(ollama).getByTestId("ollama-header-status").textContent).toBe(
        "Nothing loaded",
      ),
    );

    // The cloud card is the status tile it always was.
    expect(within(cloud).queryByTestId("ollama-panel")).toBeNull();
    expect(within(cloud).queryByTestId("ollama-header-status")).toBeNull();
    expect(cloud.className).not.toContain("col-span");

    // Manage keeps the endpoint facts and points back at the card: the
    // panel lives in one place, not two.
    fireEvent.click(within(ollama).getByRole("button", { name: "Manage" }));
    const manage = await screen.findByRole("dialog", { name: "Ollama Main" });
    expect(within(manage).queryByTestId("ollama-panel")).toBeNull();
    expect(within(manage).getByTestId("ollama-manage-note")).toBeDefined();
    expect(within(manage).getByText("http://192.168.1.79:11434/v1")).toBeDefined();
  });

  it("Use as model opens the profile dialog prefilled", async () => {
    installServer({ ollama: true });
    renderPage();
    await screen.findByRole("heading", { name: "Default model" });

    // Straight from the card's panel; no Manage dialog in between.
    const card = await screen.findByTestId("provider-card-prov-ollama");
    const row = await within(card).findByTestId("ollama-model-qwen3.8:latest");
    fireEvent.click(within(row).getByRole("button", { name: "Use as model" }));

    // A new-profile dialog that already knows the model: name, identifier,
    // the $0 price, and the host's context.
    const dialog = await screen.findByRole("dialog", { name: "New model profile" });
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

  it("treats an unpriced self-hosted profile as free and keeps its price fields empty", async () => {
    installServer({
      ollama: true,
      profiles: [profile(), ASSUMED_FREE_PROFILE],
      pricing: {
        ...PRICING,
        profiles: [
          {
            profile_id: "profile-ollama",
            display_name: "qwen3.8",
            model_name: "qwen3.8:latest",
            provider_id: "prov-ollama",
            provider_type: "ollama",
            input_cost_micros_per_million: 0,
            output_cost_micros_per_million: 0,
            price_source: "self_hosted",
            price_source_label: ASSUMED_FREE_LABEL,
            priced: true,
            assumed_free: true,
            pricing_page_url: null,
            runs_this_month: 4,
            suggestion: null,
            suggestion_label: null,
            observed: null,
          },
        ],
      },
    });
    renderPage();
    await screen.findByRole("heading", { name: "Default model" });

    // The card is calm about it: free, not "no price yet".
    const card = await screen.findByTestId("profile-card-profile-ollama");
    expect(within(card).getByText("Free (self-hosted)")).toBeDefined();
    expect(screen.queryByText("No price yet")).toBeNull();
    expect(within(card).queryByRole("button", { name: "Add price" })).toBeNull();

    // Editing says why it is free, with the fields empty and still editable
    // for an endpoint that does bill — and no "we don't know its price" box.
    fireEvent.click(within(card).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit model profile" });
    expect(within(dialog).getByText("Free (self-hosted) — no per-token price.")).toBeDefined();
    fireEvent.click(within(dialog).getByRole("button", { name: /Pricing \(auto-filled\)/ }));
    expect(within(dialog).getByText("Runs on your Ollama host — no per-token price.")).toBeDefined();
    expect(within(dialog).queryByTestId("unpriced-model-note")).toBeNull();
    const input = within(dialog).getByLabelText("Input $ / 1M tokens") as HTMLInputElement;
    const output = within(dialog).getByLabelText("Output $ / 1M tokens") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(output.value).toBe("");
    expect(input.disabled).toBe(false);
    const provenance = within(dialog).getByTestId("price-provenance");
    expect(within(provenance).getByText("Free (self-hosted)")).toBeDefined();
    expect(provenance.textContent).toContain(ASSUMED_FREE_LABEL);

    // The host's model list arriving does not write $0 into the fields: the
    // stored row already answered, and nothing gets saved that the admin did
    // not type.
    await waitFor(() => expect(within(dialog).getByText(/1 models available/)).toBeDefined());
    expect(input.value).toBe("");
    fireEvent.change(input, { target: { value: "0.5" } });
    expect(input.value).toBe("0.5");
  });

  it("calls an assumed-free default Free (self-hosted) on the hero and in the change dialog, and says whether it is loaded", async () => {
    installServer({
      ollama: true,
      profiles: [profile(), ASSUMED_FREE_PROFILE],
      defaultProfileId: "profile-ollama",
    });
    renderPage();
    const hero = await screen.findByTestId("default-model-card");
    expect(within(hero).getByText("Free (self-hosted)")).toBeDefined();
    expect(within(hero).queryByText("No price set yet")).toBeNull();

    // The hero shares the provider card's picture of the host: nothing is
    // resident, so an admin can load the default from right here.
    const state = within(hero).getByTestId("ollama-load-state");
    expect(await within(state).findByText("Not loaded")).toBeDefined();
    expect(within(state).getByRole("button", { name: "Load" })).toBeDefined();
    // …and so does the profile card for the same model.
    const card = screen.getByTestId("profile-card-profile-ollama");
    expect(within(card).getByRole("button", { name: "Load" })).toBeDefined();
    // A cloud profile says nothing about memory.
    expect(within(screen.getByTestId("profile-card-profile-1")).queryByTestId("ollama-load-state")).toBeNull();

    fireEvent.click(within(hero).getByRole("button", { name: "Change" }));
    const dialog = await screen.findByRole("dialog", { name: "Change the default model" });
    expect(
      within(within(dialog).getByTestId("default-option-profile-ollama")).getByText(
        "Free (self-hosted)",
      ),
    ).toBeDefined();
    expect(within(dialog).queryByText("No price set yet")).toBeNull();
  });

  it("prices a new profile at $0 the moment a self-hosted provider is chosen", async () => {
    installServer({ ollama: true });
    renderPage();
    await screen.findByRole("heading", { name: "Default model" });

    fireEvent.click(screen.getByRole("button", { name: /New profile/ }));
    const dialog = await screen.findByRole("dialog", { name: "New model profile" });
    fireEvent.click(within(dialog).getByRole("button", { name: /Pricing \(auto-filled\)/ }));
    const input = within(dialog).getByLabelText("Input $ / 1M tokens") as HTMLInputElement;
    const output = within(dialog).getByLabelText("Output $ / 1M tokens") as HTMLInputElement;
    // OpenAI comes first, so the dialog opens on a cloud provider: empty
    // fields behind cloud example prices.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("0.15");
    expect(output.placeholder).toBe("0.60");
    expect(
      within(dialog).getByText("No prices yet — runs will show $0.00 until you add them."),
    ).toBeDefined();

    // Choosing the Ollama host is enough — no model picked yet — for the
    // fields to hold the true price and the summary to agree with them.
    fireEvent.change(within(dialog).getByLabelText("Provider"), {
      target: { value: "prov-ollama" },
    });
    expect(input.value).toBe("0");
    expect(output.value).toBe("0");
    expect(input.placeholder).toBe("0");
    expect(output.placeholder).toBe("0");
    expect(within(dialog).getByText("$0 in · $0 out per 1M tokens")).toBeDefined();
    expect(within(dialog).getByText("Runs on your Ollama host — no per-token price.")).toBeDefined();
    // The host's model list arriving later leaves the $0 alone.
    await waitFor(() => expect(within(dialog).getByText(/1 models available/)).toBeDefined());
    expect(input.value).toBe("0");

    // Back on a cloud provider the $0 goes too, so a cloud model is never
    // saved as free by accident.
    fireEvent.change(within(dialog).getByLabelText("Provider"), { target: { value: "prov-1" } });
    expect(input.value).toBe("");
    expect(output.value).toBe("");
    expect(input.placeholder).toBe("0.15");
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
