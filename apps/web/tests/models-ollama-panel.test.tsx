/** Render tests for the local-models panel an Ollama provider shows instead
 * of a balance: the installed list, the loaded facts, the load flow that
 * stays "loading" until the poll confirms it, unload, the host's own refusal
 * sentence, the unreachable and empty states, the viewer view, and the
 * hand-over into a prefilled profile. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OllamaPanel } from "@/components/models/ollama-panel";
import { api, ApiError } from "@/lib/api";
import { profilePrefillForOllamaModel } from "@/lib/models";
import type { ModelProvider, OllamaLoaded, OllamaLoadResult, OllamaModels } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const PROVIDER: ModelProvider = {
  id: "prov-1",
  workspace_id: "w1",
  type: "ollama",
  display_name: "Ollama Main",
  base_url: "http://192.168.1.79:11434/v1",
  secret_id: null,
  credits_loaded_micros: null,
  has_admin_key: false,
  enabled: true,
  last_verified_at: "2026-09-01T00:00:00Z",
  last_error: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

const MUSE = {
  name: "muse-glimmer:latest",
  size_bytes: 18_200_000_000,
  family: "llama",
  parameter_size: "27.9B",
  quantization: "Q4_K_M",
  modified_at: "2026-08-30T19:34:56.123456Z",
  context_length: 131_072,
  capabilities: ["completion", "tools"],
  loaded: true,
  size_vram_bytes: 18_200_000_000,
  expires_at: new Date(Date.now() + 4 * 60_000).toISOString(),
  keeps_loaded: false,
};

const QWEN = {
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

const MUSE_LOADED = {
  name: MUSE.name,
  size_bytes: MUSE.size_bytes,
  size_vram_bytes: MUSE.size_vram_bytes,
  expires_at: MUSE.expires_at,
  keeps_loaded: false,
  context_length: 8192,
};

const QWEN_LOADED = {
  name: QWEN.name,
  size_bytes: QWEN.size_bytes,
  size_vram_bytes: QWEN.size_vram_bytes ?? 17_700_000_000,
  expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
  keeps_loaded: false,
  context_length: 32_768,
};

function listing(overrides: Partial<OllamaModels> = {}): OllamaModels {
  return { models: [MUSE, QWEN], detail: null, fetched_at: "2026-09-02T10:00:00Z", ...overrides };
}

function loaded(models: OllamaLoaded["models"], detail: string | null = null): OllamaLoaded {
  return { models, detail, fetched_at: "2026-09-02T10:00:00Z" };
}

const LOAD_URL = "/api/v1/workspaces/w1/model-providers/prov-1/ollama/load";
const UNLOAD_URL = "/api/v1/workspaces/w1/model-providers/prov-1/ollama/unload";

/** A stand-in API whose loaded set the test can change between polls. */
function installApi(options: {
  listing?: OllamaModels | Error;
  loaded?: OllamaLoaded["models"];
  load?: (body: { model: string; keep_alive: string }) => OllamaLoadResult;
  unload?: (body: { model: string }) => OllamaLoadResult;
} = {}) {
  const state = { loaded: options.loaded ?? [MUSE_LOADED], loadedCalls: 0 };
  vi.mocked(api).mockImplementation(
    async (path: string, requestOptions?: { method?: string; body?: unknown }) => {
      if (path.endsWith("/ollama/models")) {
        if (options.listing instanceof Error) throw options.listing;
        return options.listing ?? listing();
      }
      if (path.endsWith("/ollama/loaded")) {
        state.loadedCalls += 1;
        return loaded(state.loaded);
      }
      if (path === LOAD_URL && options.load) {
        return options.load(requestOptions?.body as { model: string; keep_alive: string });
      }
      if (path === UNLOAD_URL && options.unload) {
        return options.unload(requestOptions?.body as { model: string });
      }
      throw new Error(`Unexpected request: ${requestOptions?.method ?? "GET"} ${path}`);
    },
  );
  return state;
}

function renderPanel(overrides: { isAdmin?: boolean } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onError = vi.fn();
  const onUseAsModel = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <OllamaPanel
        workspaceId="w1"
        provider={PROVIDER}
        isAdmin={overrides.isAdmin ?? true}
        onError={onError}
        onUseAsModel={onUseAsModel}
      />
    </QueryClientProvider>,
  );
  /** What the ten-second poll does, without waiting ten seconds. */
  const poll = () =>
    act(() => queryClient.invalidateQueries({ queryKey: ["ollama-loaded", "w1"] }));
  return { onError, onUseAsModel, poll };
}

describe("OllamaPanel", () => {
  it("lists local models with size, family, params, quant and context", async () => {
    installApi();
    renderPanel();
    const qwen = await screen.findByTestId("ollama-model-qwen3.8:latest");
    expect(within(qwen).getByText("qwen3.8:latest")).toBeTruthy();
    expect(within(qwen).getByText("17.7 GB · qwen3 · 27.3B · Q4_K_M · ctx 41.0k")).toBeTruthy();
    // Capabilities other than plain completion are worth a chip.
    expect(within(qwen).getByText("Tools")).toBeTruthy();
    expect(within(qwen).getByText("Thinking")).toBeTruthy();
    expect(screen.getByText("Ollama — 2 models, 1 loaded")).toBeTruthy();
  });

  it("marks the loaded model with VRAM and expiry, loaded rows first", async () => {
    installApi();
    renderPanel();
    const muse = await screen.findByTestId("ollama-model-muse-glimmer:latest");
    expect(within(muse).getByText("Loaded")).toBeTruthy();
    expect(within(muse).getByText(/18\.2 GB VRAM · expires in 4 minutes/)).toBeTruthy();
    const rows = screen.getAllByTestId(/^ollama-model-/);
    expect(rows[0].getAttribute("data-testid")).toBe("ollama-model-muse-glimmer:latest");
    expect(within(rows[0]).getByRole("button", { name: "Unload" })).toBeTruthy();
    expect(within(rows[1]).getByRole("button", { name: "Load" })).toBeTruthy();
  });

  it("says a model kept forever stays loaded rather than expiring in centuries", async () => {
    installApi({ loaded: [{ ...MUSE_LOADED, keeps_loaded: true, expires_at: "2292-09-02T10:00:00Z" }] });
    renderPanel();
    const muse = await screen.findByTestId("ollama-model-muse-glimmer:latest");
    expect(within(muse).getByText(/stays loaded/)).toBeTruthy();
    expect(within(muse).queryByText(/expires/)).toBeNull();
  });

  it("loads with the chosen keep_alive and shows progress until the poll reports it", async () => {
    const state = installApi({
      load: (body) => ({
        ok: true,
        status: "loading",
        model: body.model,
        keep_alive: body.keep_alive,
        detail: "Ollama is still loading qwen3.8:latest. It will show as loaded when it finishes.",
      }),
    });
    const { poll } = renderPanel();
    const qwen = await screen.findByTestId("ollama-model-qwen3.8:latest");

    fireEvent.change(screen.getByLabelText("Keep loaded for"), { target: { value: "1h" } });
    fireEvent.click(within(qwen).getByRole("button", { name: "Load" }));

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(LOAD_URL, {
        method: "POST",
        body: { model: "qwen3.8:latest", keep_alive: "1h" },
      }),
    );
    // The API handed the load off; the row stays in progress and says how
    // big the wait is.
    expect(
      within(qwen).getByText(/Loading qwen3\.8 — 17\.7 GB, this can take a minute or more\./),
    ).toBeTruthy();
    expect(within(qwen).getByRole("button", { name: "Loading…" }).hasAttribute("disabled")).toBe(
      true,
    );
    await waitFor(() => expect(state.loadedCalls).toBeGreaterThanOrEqual(2));
    expect(within(qwen).queryByText("Loaded")).toBeNull();

    // The host finishes; the next poll flips the row.
    state.loaded = [MUSE_LOADED, QWEN_LOADED];
    await poll();
    await within(qwen).findByText("Loaded");
    expect(within(qwen).queryByText(/this can take a minute/)).toBeNull();
    expect(within(qwen).getByRole("button", { name: "Unload" })).toBeTruthy();
    expect(screen.getByText("Ollama — 2 models, 2 loaded")).toBeTruthy();
  });

  it("unloads through /ollama/unload", async () => {
    const state = installApi({
      unload: (body) => {
        state.loaded = [];
        return {
          ok: true,
          status: "unloaded",
          model: body.model,
          keep_alive: null,
          detail: `${body.model} was unloaded.`,
        };
      },
    });
    renderPanel();
    const muse = await screen.findByTestId("ollama-model-muse-glimmer:latest");
    fireEvent.click(within(muse).getByRole("button", { name: "Unload" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(UNLOAD_URL, {
        method: "POST",
        body: { model: "muse-glimmer:latest" },
      }),
    );
    await waitFor(() => expect(within(muse).queryByText("Loaded")).toBeNull());
    expect(within(muse).getByRole("button", { name: "Load" })).toBeTruthy();
  });

  it("shows Ollama's own sentence when a load is refused", async () => {
    installApi({
      load: (body) => ({
        ok: false,
        status: "failed",
        model: body.model,
        keep_alive: body.keep_alive,
        detail: "ollama: HTTP 404: model 'qwen3.8:latest' not found, try pulling it first",
      }),
    });
    const { onError } = renderPanel();
    const qwen = await screen.findByTestId("ollama-model-qwen3.8:latest");
    fireEvent.click(within(qwen).getByRole("button", { name: "Load" }));
    const note = await within(qwen).findByRole("alert");
    expect(note.textContent).toBe(
      "ollama: HTTP 404: model 'qwen3.8:latest' not found, try pulling it first",
    );
    // The refusal stays on the row; the progress line and the dialog-level
    // error are both cleared.
    expect(within(qwen).queryByText(/this can take a minute/)).toBeNull();
    expect(within(qwen).getByRole("button", { name: "Load" })).toBeTruthy();
    expect(onError).toHaveBeenLastCalledWith(null);
  });

  it("hands a request failure to the dialog's error note", async () => {
    installApi();
    vi.mocked(api).mockImplementationOnce(async () => listing());
    const { onError } = renderPanel();
    const qwen = await screen.findByTestId("ollama-model-qwen3.8:latest");
    vi.mocked(api).mockRejectedValueOnce(new ApiError(409, "This provider is not an Ollama endpoint"));
    fireEvent.click(within(qwen).getByRole("button", { name: "Load" }));
    await waitFor(() =>
      expect(onError).toHaveBeenLastCalledWith("This provider is not an Ollama endpoint"),
    );
    expect(within(qwen).queryByText(/this can take a minute/)).toBeNull();
  });

  it("explains an unreachable host with a Retry", async () => {
    installApi({ listing: new ApiError(502, "upstream unreachable") });
    renderPanel();
    const panel = await screen.findByTestId("ollama-panel");
    await within(panel).findByText(/We couldn't reach Ollama at/);
    expect(within(panel).getByText("http://192.168.1.79:11434/v1")).toBeTruthy();
    expect(within(panel).getByText(/Check the host is up and the base URL is right/)).toBeTruthy();

    installApi();
    fireEvent.click(within(panel).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("ollama-model-qwen3.8:latest")).toBeTruthy();
  });

  it("shows the empty state when nothing is installed", async () => {
    installApi({ listing: listing({ models: [] }), loaded: [] });
    renderPanel();
    expect(await screen.findByText(/No models on this Ollama host yet/)).toBeTruthy();
    expect(screen.getByText("ollama pull qwen3")).toBeTruthy();
    expect(screen.queryByText(/models, /)).toBeNull();
  });

  it("repeats the host's reason when the list came back empty because of it", async () => {
    installApi({
      listing: listing({ models: [], detail: "ollama: network error: ConnectError" }),
      loaded: [],
    });
    renderPanel();
    expect(
      await screen.findByText("Couldn't list models — ollama: network error: ConnectError"),
    ).toBeTruthy();
  });

  it("hides Load/Unload/Use as model from viewers", async () => {
    installApi();
    renderPanel({ isAdmin: false });
    const muse = await screen.findByTestId("ollama-model-muse-glimmer:latest");
    expect(within(muse).getByText("Loaded")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Unload" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Use as model" })).toBeNull();
    expect(screen.queryByLabelText("Keep loaded for")).toBeNull();
  });

  it("Use as model hands over a $0 prefill", async () => {
    installApi();
    const { onUseAsModel } = renderPanel();
    const qwen = await screen.findByTestId("ollama-model-qwen3.8:latest");
    fireEvent.click(within(qwen).getByRole("button", { name: "Use as model" }));
    expect(onUseAsModel).toHaveBeenCalledWith(profilePrefillForOllamaModel("prov-1", QWEN));
    expect(onUseAsModel.mock.calls[0][0]).toEqual({
      providerId: "prov-1",
      modelName: "qwen3.8:latest",
      displayName: "qwen3.8",
      contextWindow: 40_960,
      inputCostMicros: 0,
      outputCostMicros: 0,
    });
  });
});
