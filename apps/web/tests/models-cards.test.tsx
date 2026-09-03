/** Render tests for the recomposed Models page pieces: the default-model
 * hero card, the model rows (ProfileCard), the provider status rows
 * (ProviderCard), and the manage dialog, including that destructive actions
 * go through the shared ConfirmDialog rather than window.confirm. The Ollama
 * host's own block is covered in models-ollama-panel.test.tsx. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChangeDefaultDialog } from "@/components/models/change-default-dialog";
import { DefaultModelCard } from "@/components/models/default-model-card";
import { ProfileCard } from "@/components/models/profile-card";
import { ProviderCard } from "@/components/models/provider-card";
import { ProviderManageDialog } from "@/components/models/provider-manage-dialog";
import { api, ApiError } from "@/lib/api";
import type {
  ModelProfile,
  ModelProvider,
  OllamaLoaded,
  OllamaLoadedModel,
  OllamaLoadResult,
  OllamaModel,
  OllamaModels,
  ProviderBalance,
} from "@/lib/types";
import { WithOllamaHost } from "@/tests/helpers/ollama-host";

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

const ollamaProvider = provider({
  id: "prov-ollama",
  type: "ollama",
  display_name: "Ollama Main",
  secret_id: null,
  base_url: "http://192.168.1.79:11434/v1",
});

/** A local model turned into a profile and left unpriced: the API reports it
 * as assumed free rather than as a gap. */
const ollamaProfile = profile({
  id: "p-ollama",
  provider_id: "prov-ollama",
  model_name: "qwen3.8:latest",
  display_name: "Qwen 3.8",
  context_window: 40_960,
  input_cost_micros_per_million: null,
  output_cost_micros_per_million: null,
  price_source: null,
  assumed_free: true,
});

const MUSE: OllamaModel = {
  name: "muse-glimmer:latest",
  size_bytes: 18_200_000_000,
  family: "llama",
  parameter_size: "27.9B",
  quantization: "Q4_K_M",
  modified_at: "2026-08-30T19:34:56Z",
  context_length: 131_072,
  capabilities: ["completion", "tools"],
  loaded: false,
  size_vram_bytes: null,
  expires_at: null,
  keeps_loaded: false,
};

const QWEN: OllamaModel = {
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
};

/** Qwen resident with four minutes left on its lease. */
const QWEN_LOADED: OllamaLoadedModel = {
  name: QWEN.name,
  size_bytes: QWEN.size_bytes,
  size_vram_bytes: 17_500_000_000,
  expires_at: new Date(Date.now() + 4 * 60_000).toISOString(),
  keeps_loaded: false,
  context_length: 32_768,
};

function ollamaListing(models: OllamaModel[] = [MUSE, QWEN]): OllamaModels {
  return { models, detail: null, fetched_at: "2026-09-02T10:00:00Z" };
}

function ollamaLoaded(models: OllamaLoadedModel[], detail: string | null = null): OllamaLoaded {
  return { models, detail, fetched_at: "2026-09-02T10:00:00Z" };
}

const LOAD_URL = "/api/v1/workspaces/w1/model-providers/prov-ollama/ollama/load";

/** A stand-in API for one Ollama host whose loaded set the test can change
 * between polls. */
function installOllamaApi(
  options: {
    listing?: OllamaModels | Error;
    loaded?: OllamaLoadedModel[] | Error;
    detail?: string | null;
    load?: (body: { model: string; keep_alive: string }) => OllamaLoadResult;
  } = {},
) {
  const state = {
    loaded: options.loaded instanceof Error ? [] : (options.loaded ?? []),
    loadedCalls: 0,
  };
  vi.mocked(api).mockImplementation(
    async (path: string, requestOptions?: { method?: string; body?: unknown }) => {
      if (path.endsWith("/ollama/models")) {
        if (options.listing instanceof Error) throw options.listing;
        return options.listing ?? ollamaListing();
      }
      if (path.endsWith("/ollama/loaded")) {
        if (options.loaded instanceof Error) throw options.loaded;
        state.loadedCalls += 1;
        return ollamaLoaded(state.loaded, options.detail ?? null);
      }
      if (path === LOAD_URL && options.load) {
        return options.load(requestOptions?.body as { model: string; keep_alive: string });
      }
      throw new Error(`Unexpected request: ${requestOptions?.method ?? "GET"} ${path}`);
    },
  );
  return state;
}

function newQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderWithQuery(ui: React.ReactElement) {
  return render(<QueryClientProvider client={newQueryClient()}>{ui}</QueryClientProvider>);
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

  it("offers Choose a default on the empty state for admins, not viewers", () => {
    const onChange = vi.fn();
    renderWithQuery(<DefaultModelCard profile={null} provider={null} isAdmin onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Choose a default" }));
    expect(onChange).toHaveBeenCalled();

    cleanup();
    renderWithQuery(
      <DefaultModelCard profile={null} provider={null} isAdmin={false} onChange={onChange} />,
    );
    expect(screen.getByText("No default model yet")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Choose a default" })).toBeNull();
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

  it("calls an assumed-free default Free (self-hosted), in the model card's words", () => {
    renderWithQuery(
      <DefaultModelCard
        profile={ollamaProfile}
        provider={ollamaProvider}
        isAdmin
        onChange={() => undefined}
      />,
    );
    expect(screen.getByText("Free (self-hosted)")).toBeTruthy();
    expect(screen.queryByText("No price set yet")).toBeNull();
  });

  it("still warns about a cloud default with no price", () => {
    renderWithQuery(
      <DefaultModelCard
        profile={profile({
          input_cost_micros_per_million: null,
          output_cost_micros_per_million: null,
          price_source: null,
          assumed_free: false,
        })}
        provider={provider()}
        isAdmin
        onChange={() => undefined}
      />,
    );
    expect(screen.getByText("No price set yet")).toBeTruthy();
    expect(screen.queryByText("Free (self-hosted)")).toBeNull();
  });

  it("says whether an Ollama default is loaded, with the lease left", async () => {
    installOllamaApi({ loaded: [QWEN_LOADED] });
    renderWithQuery(
      <WithOllamaHost workspaceId="w1" providerId="prov-ollama">
        {(host) => (
          <DefaultModelCard
            profile={ollamaProfile}
            provider={ollamaProvider}
            isAdmin
            host={host}
            onChange={() => undefined}
          />
        )}
      </WithOllamaHost>,
    );
    const hero = screen.getByTestId("default-model-card");
    const state = within(hero).getByTestId("ollama-load-state");
    expect(await within(state).findByText("Loaded")).toBeTruthy();
    expect(within(state).getByText("for 4 more minutes")).toBeTruthy();
    expect(within(state).queryByRole("button", { name: "Load" })).toBeNull();
  });
});

describe("ProfileCard", () => {
  const cardProps = {
    provider: provider(),
    workspaceId: "w1",
    pricing: undefined,
    pricingPages: undefined,
    onChanged: () => undefined,
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
          assumed_free: false,
        })}
        isDefault={false}
        isAdmin
      />,
    );
    expect(screen.getByText("No price yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add price" })).toBeTruthy();
    expect(screen.queryByTestId("profile-cost-line")).toBeNull();
    expect(screen.queryByText("Free (self-hosted)")).toBeNull();
  });

  it("shows a self-hosted profile with no stored price as free, not unpriced", () => {
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        provider={ollamaProvider}
        profile={ollamaProfile}
        isDefault={false}
        isAdmin
      />,
    );
    expect(screen.getByText("Free (self-hosted)")).toBeTruthy();
    expect(screen.queryByText("No price yet")).toBeNull();
    // Nothing is missing, so there is nothing to add from the card; the edit
    // dialog keeps the fields for an endpoint that does bill.
    expect(screen.queryByRole("button", { name: "Add price" })).toBeNull();
    expect(screen.queryByTestId("profile-cost-line")).toBeNull();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
    // Without the page's host subscription the card says nothing about
    // memory: no state is better than a guess.
    expect(screen.queryByTestId("ollama-load-state")).toBeNull();
  });

  it("shows the stored price on a self-hosted profile an admin has priced", () => {
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        provider={ollamaProvider}
        profile={profile({
          provider_id: "prov-ollama",
          model_name: "qwen3.8:latest",
          display_name: "qwen3.8",
          input_cost_micros_per_million: 0,
          output_cost_micros_per_million: 0,
          price_source: "user",
          assumed_free: false,
        })}
        isDefault={false}
        isAdmin
      />,
    );
    expect(screen.getByTestId("profile-cost-line").textContent).toContain("$0.00 in · $0.00 out");
    expect(screen.queryByText("Free (self-hosted)")).toBeNull();
    expect(screen.queryByText("No price yet")).toBeNull();
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

  it("shows the raw model id on the facts line when it differs from the name", () => {
    renderWithQuery(<ProfileCard {...cardProps} profile={profile()} isDefault={false} isAdmin />);
    // Touch users and viewers get the id without a hover, in mono.
    const id = screen.getByText("gpt-5-mini");
    expect(id.className).toContain("font-mono");
    expect(screen.getByText("OpenAI")).toBeTruthy();

    // A row named after its id does not say it twice.
    cleanup();
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        profile={profile({ display_name: "gpt-5-mini" })}
        isDefault={false}
        isAdmin
      />,
    );
    expect(screen.getAllByText("gpt-5-mini")).toHaveLength(1);
  });

  it("falls back to the provider type label for a member who cannot list providers", () => {
    renderWithQuery(
      <ProfileCard
        {...cardProps}
        provider={undefined}
        pricing={{
          profile_id: "p1",
          display_name: "GPT-5 mini",
          model_name: "gpt-5-mini",
          provider_id: "prov-1",
          provider_type: "openai",
          input_cost_micros_per_million: 2_500_000,
          output_cost_micros_per_million: 10_000_000,
          price_source: "catalog",
          price_source_label: "Public list price",
          priced: true,
          pricing_page_url: null,
          runs_this_month: 0,
          suggestion: null,
          suggestion_label: null,
          observed: null,
        }}
        profile={profile()}
        isDefault={false}
        isAdmin={false}
      />,
    );
    expect(screen.getByText("OpenAI")).toBeTruthy();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("keeps a failed Make default beside the row", async () => {
    vi.mocked(api).mockRejectedValue(new ApiError(500, "boom"));
    renderWithQuery(<ProfileCard {...cardProps} profile={profile()} isDefault={false} isAdmin />);
    fireEvent.click(screen.getByRole("button", { name: "Make default" }));
    const row = screen.getByTestId("profile-card-p1");
    const note = await within(row).findByRole("alert");
    expect(note.textContent).toBe("boom");
  });

  /** The card with the page's subscription to its Ollama host. */
  function renderOllamaProfile(overrides: { isAdmin?: boolean; modelName?: string } = {}) {
    const queryClient = newQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <WithOllamaHost workspaceId="w1" providerId="prov-ollama">
          {(host) => (
            <ProfileCard
              {...cardProps}
              provider={ollamaProvider}
              host={host}
              profile={
                overrides.modelName
                  ? { ...ollamaProfile, model_name: overrides.modelName }
                  : ollamaProfile
              }
              isDefault={false}
              isAdmin={overrides.isAdmin ?? true}
            />
          )}
        </WithOllamaHost>
      </QueryClientProvider>,
    );
    /** What the ten-second poll does, without waiting ten seconds. */
    const poll = () =>
      act(() => queryClient.invalidateQueries({ queryKey: ["ollama-loaded", "w1"] }));
    return { poll };
  }

  it("shows Loaded with the lease left when its model is resident", async () => {
    installOllamaApi({ loaded: [QWEN_LOADED] });
    renderOllamaProfile();
    const state = screen.getByTestId("ollama-load-state");
    expect(await within(state).findByText("Loaded")).toBeTruthy();
    expect(within(state).getByText("for 4 more minutes")).toBeTruthy();
    expect(within(state).queryByRole("button", { name: "Load" })).toBeNull();
  });

  it("says a model kept forever stays loaded", async () => {
    installOllamaApi({
      loaded: [{ ...QWEN_LOADED, keeps_loaded: true, expires_at: "2292-09-02T10:00:00Z" }],
    });
    renderOllamaProfile();
    const state = screen.getByTestId("ollama-load-state");
    expect(await within(state).findByText("Loaded")).toBeTruthy();
    expect(within(state).getByText("stays loaded")).toBeTruthy();
  });

  it("matches a profile typed without the :latest tag to the host's row", async () => {
    installOllamaApi({ loaded: [QWEN_LOADED] });
    renderOllamaProfile({ modelName: "qwen3.8" });
    expect(await within(screen.getByTestId("ollama-load-state")).findByText("Loaded")).toBeTruthy();
  });

  it("offers Load when the model is not resident, shows its size while it loads, and flips when the poll confirms", async () => {
    const api_ = installOllamaApi({
      load: (body) => ({
        ok: true,
        status: "loading",
        model: body.model,
        keep_alive: body.keep_alive,
        detail: "Ollama is still loading qwen3.8:latest.",
      }),
    });
    const { poll } = renderOllamaProfile();
    const state = screen.getByTestId("ollama-load-state");
    expect(await within(state).findByText("Not loaded")).toBeTruthy();

    fireEvent.click(within(state).getByRole("button", { name: "Load" }));
    // Same request and the same default lease the panel sends.
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(LOAD_URL, {
        method: "POST",
        body: { model: "qwen3.8:latest", keep_alive: "5m" },
      }),
    );
    // Optimistic from the click: the button gives way to progress with the
    // weight the listing reports, and stays there until the poll confirms.
    await waitFor(() =>
      expect(within(state).getByRole("status").textContent).toBe("Loading — 17.7 GB"),
    );
    expect(within(state).queryByRole("button", { name: "Load" })).toBeNull();
    await waitFor(() => expect(api_.loadedCalls).toBeGreaterThanOrEqual(2));
    expect(within(state).queryByText("Loaded")).toBeNull();

    api_.loaded = [QWEN_LOADED];
    await poll();
    expect(await within(state).findByText("Loaded")).toBeTruthy();
    expect(within(state).queryByRole("status")).toBeNull();
  });

  it("keeps the host's refusal beside the control", async () => {
    installOllamaApi({
      load: (body) => ({
        ok: false,
        status: "failed",
        model: body.model,
        keep_alive: body.keep_alive,
        detail: "ollama: HTTP 404: model 'qwen3.8:latest' not found, try pulling it first",
      }),
    });
    renderOllamaProfile();
    const state = screen.getByTestId("ollama-load-state");
    fireEvent.click(await within(state).findByRole("button", { name: "Load" }));
    const note = await within(state).findByRole("alert");
    expect(note.textContent).toBe(
      "ollama: HTTP 404: model 'qwen3.8:latest' not found, try pulling it first",
    );
    expect(within(state).getByRole("button", { name: "Load" })).toBeTruthy();
    expect(within(state).queryByRole("status")).toBeNull();
  });

  it("says when the host does not have the model at all", async () => {
    installOllamaApi({ listing: ollamaListing([MUSE]) });
    renderOllamaProfile();
    const state = screen.getByTestId("ollama-load-state");
    expect(await within(state).findByText("Not installed on the host")).toBeTruthy();
    expect(within(state).queryByRole("button", { name: "Load" })).toBeNull();
  });

  it("shows viewers the loaded state without a Load button", async () => {
    installOllamaApi();
    renderOllamaProfile({ isAdmin: false });
    const state = screen.getByTestId("ollama-load-state");
    expect(await within(state).findByText("Not loaded")).toBeTruthy();
    expect(within(state).queryByRole("button", { name: "Load" })).toBeNull();
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

  it("shows the provider's spend this month when given", () => {
    renderWithQuery(
      <ProviderCard
        provider={provider()}
        typeLabel="OpenAI"
        profileCount={2}
        spentMonthMicros={10_000_000}
        onManage={() => undefined}
      />,
    );
    expect(screen.getByText("$10.00 this month")).toBeTruthy();

    cleanup();
    renderWithQuery(
      <ProviderCard provider={provider()} typeLabel="OpenAI" profileCount={2} onManage={() => undefined} />,
    );
    expect(screen.queryByText(/this month/)).toBeNull();
  });

  it("is a status row for an Ollama host too: the live state lives under Local models", () => {
    renderWithQuery(
      <ProviderCard
        provider={ollamaProvider}
        typeLabel="Ollama (local)"
        profileCount={1}
        onManage={() => undefined}
      />,
    );
    const row = screen.getByTestId("provider-card-prov-ollama");
    expect(within(row).getByText("Ollama (local)")).toBeTruthy();
    expect(within(row).getByText("1 model")).toBeTruthy();
    expect(within(row).getByRole("button", { name: "Manage" })).toBeTruthy();
    expect(within(row).queryByTestId("ollama-panel")).toBeNull();
    expect(within(row).queryByTestId("ollama-header-status")).toBeNull();
  });
});

describe("ProviderManageDialog", () => {
  function renderDialog(overrides: { isAdmin?: boolean; provider?: ModelProvider } = {}) {
    vi.mocked(api).mockImplementation(async (path: string, options?: { method?: string }) => {
      if (path.endsWith("/balance")) return balance();
      if (options?.method === "DELETE") return undefined;
      throw new Error(`Unexpected request: ${options?.method ?? "GET"} ${path}`);
    });
    const onChanged = vi.fn();
    const onClose = vi.fn();
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
      />,
    );
    return { onChanged, onClose };
  }

  it("points an Ollama provider at Local models instead of a balance block or a second panel", async () => {
    renderDialog({ provider: ollamaProvider });
    expect(await screen.findByTestId("ollama-manage-note")).toBeTruthy();
    expect(screen.queryByTestId("balance-block")).toBeNull();
    expect(screen.queryByTestId("ollama-panel")).toBeNull();
    // The dialog asks the host nothing: the card already holds that picture.
    expect(vi.mocked(api)).not.toHaveBeenCalled();
    // The endpoint facts are still what Manage is for.
    expect(screen.getByText("http://192.168.1.79:11434/v1")).toBeTruthy();
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

  it("calls an assumed-free option Free (self-hosted) and still warns about a cloud one with no price", () => {
    renderWithQuery(
      <ChangeDefaultDialog
        workspaceId="w1"
        profiles={[
          profile(),
          ollamaProfile,
          profile({
            id: "p-unpriced",
            display_name: "Mystery",
            input_cost_micros_per_million: null,
            output_cost_micros_per_million: null,
            price_source: null,
            assumed_free: false,
          }),
        ]}
        providers={[provider(), ollamaProvider]}
        currentDefaultId="p1"
        onClose={() => undefined}
        onChanged={() => undefined}
      />,
    );
    const free = screen.getByTestId("default-option-p-ollama");
    expect(within(free).getByText("Free (self-hosted)")).toBeTruthy();
    expect(within(free).queryByText("No price set yet")).toBeNull();
    expect(
      within(screen.getByTestId("default-option-p-unpriced")).getByText("No price set yet"),
    ).toBeTruthy();
  });
});
