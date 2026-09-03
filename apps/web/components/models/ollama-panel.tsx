"use client";

/** One Ollama host's local models, as its own block under "Local models" on
 * the Models page. There is no bill to show for a local host, so the block
 * answers the questions an admin has instead: what is installed, what is
 * resident in memory right now, and the two things worth doing about it —
 * load a model ahead of a conversation so the first reply does not wait on
 * 18 GB of weights, and unload one to make room for another.
 *
 * It reads and acts through the page's one subscription to the host
 * (lib/ollama-host.ts) rather than querying on its own, so the model rows
 * above it, the default hero, and the status line in its own header always
 * agree with its rows. */

import { HardDrive } from "lucide-react";
import { Fragment, useState } from "react";
import { Badge, Button, ErrorNote } from "@/components/ui";
import { errorText } from "@/lib/api";
import { formatTokens } from "@/lib/format";
import { useCountdownTick, type OllamaHost } from "@/lib/ollama-host";
import {
  formatModelSize,
  OLLAMA_DEFAULT_KEEP_ALIVE,
  OLLAMA_KEEP_ALIVE_OPTIONS,
  ollamaDisplayName,
  ollamaLeaseText,
  ollamaLoadedSummary,
  ollamaMemoryText,
  ollamaUsedAsText,
  profilePrefillForOllamaModel,
  type ProfilePrefill,
} from "@/lib/models";
import type { ModelProfile, ModelProvider, OllamaKeepAlive, OllamaModel } from "@/lib/types";

/** Size, family, parameter count, quantisation and context — whichever of
 * them the host reported — as one quiet line. */
function metaLine(model: OllamaModel): string {
  return [
    formatModelSize(model.size_bytes),
    model.family,
    model.parameter_size,
    model.quantization,
    model.context_length ? `ctx ${formatTokens(model.context_length)}` : null,
  ]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
}

/** Every chat model reports "completion", so it says nothing; the rest are
 * the facts that decide whether an agent can use the model at all. */
const CAPABILITY_LABELS: Record<string, string> = {
  tools: "Tools",
  thinking: "Thinking",
  vision: "Vision",
  embedding: "Embeddings",
  insert: "Fill-in",
};

function capabilityLabels(capabilities: string[]): string[] {
  return capabilities
    .filter((capability) => capability !== "completion")
    .map((capability) => CAPABILITY_LABELS[capability] ?? capability);
}

function without(record: Record<string, string>, key: string): Record<string, string> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

const SUMMARY_TONE = {
  ok: "text-ok",
  neutral: "text-faint",
  danger: "text-danger",
} as const;

/** One line in the block's header saying what is resident right now. It
 * comes from the ten-second poll, not the listing, so it stays true when
 * the rows beneath cannot list the host. */
function OllamaHeaderStatus({ host }: { host: OllamaHost }) {
  const summary = ollamaLoadedSummary(
    host.loaded.data,
    host.loaded.isError,
    host.models.data?.models.length ?? null,
  );
  if (!summary) {
    return (
      <p data-testid="ollama-header-status" className="ml-auto truncate text-xs text-faint">
        Checking what&apos;s loaded…
      </p>
    );
  }
  return (
    <p
      data-testid="ollama-header-status"
      title={summary.detail ?? summary.text}
      className={`ml-auto truncate text-xs ${SUMMARY_TONE[summary.tone]}`}
    >
      {summary.text}
    </p>
  );
}

/** The block plus the note for a request that failed outright (network, a
 * provider that is not an Ollama endpoint); the host's own refusals stay on
 * their rows. */
export function OllamaHostSection({
  provider,
  host,
  isAdmin,
  profiles,
  onUseAsModel,
}: {
  provider: ModelProvider;
  host: OllamaHost;
  isAdmin: boolean;
  /** Every profile in the workspace; the rows name the ones that run on
   * them. */
  profiles: ModelProfile[];
  onUseAsModel: (prefill: ProfilePrefill) => void;
}) {
  const [panelError, setPanelError] = useState<string | null>(null);
  return (
    <div data-testid={`ollama-host-${provider.id}`} className="space-y-2">
      <OllamaPanel
        provider={provider}
        host={host}
        isAdmin={isAdmin}
        profiles={profiles}
        onError={setPanelError}
        onUseAsModel={onUseAsModel}
      />
      <ErrorNote message={panelError} />
    </div>
  );
}

export function OllamaPanel({
  provider,
  host,
  isAdmin,
  profiles = [],
  onError,
  onUseAsModel,
}: {
  provider: ModelProvider;
  /** The page's subscription to this host — see lib/ollama-host.ts. */
  host: OllamaHost;
  isAdmin: boolean;
  /** Profiles that may run on this host, so a row can say `used as “…”`. */
  profiles?: ModelProfile[];
  /** Request failures go to the section's ErrorNote, under the block. */
  onError: (message: string | null) => void;
  /** Opens the profile dialog prefilled for the picked model. */
  onUseAsModel: (prefill: ProfilePrefill) => void;
}) {
  const { models, loaded, resident, pending, unloading } = host;
  const [keepAlive, setKeepAlive] = useState<OllamaKeepAlive>(OLLAMA_DEFAULT_KEEP_ALIVE);
  /** The host's own refusal for a row ("model 'x' not found…"), kept beside
   * the row rather than in a note that disappears. */
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  const rows = models.data?.models ?? [];

  // "for 4 more minutes" must count down between polls, but only a row
  // with an expiry needs the clock running.
  useCountdownTick(
    rows.some((row) => {
      const facts = resident.get(row.name);
      return facts !== undefined && !facts.keepsLoaded && facts.expiresAt !== null;
    }),
  );

  const requestLoad = (name: string) => {
    onError(null);
    setRowErrors((current) => without(current, name));
    host.load(name, keepAlive).then(
      (result) => {
        if (!result.ok) setRowErrors((current) => ({ ...current, [name]: result.detail }));
      },
      (error: unknown) => onError(errorText(error, "Loading the model failed.")),
    );
  };

  const requestUnload = (name: string) => {
    onError(null);
    setRowErrors((current) => without(current, name));
    host.unload(name).then(
      (result) => {
        if (!result.ok) setRowErrors((current) => ({ ...current, [name]: result.detail }));
      },
      (error: unknown) => onError(errorText(error, "Unloading the model failed.")),
    );
  };

  const sorted = [...rows].sort((a, b) => {
    const aLoaded = resident.has(a.name) ? 0 : 1;
    const bLoaded = resident.has(b.name) ? 0 : 1;
    return aLoaded - bLoaded || a.name.localeCompare(b.name);
  });
  const version = models.data?.version;
  // The loaded count lives once, in the status line beside this caption.
  const caption =
    sorted.length > 0
      ? `Ollama${version ? ` ${version}` : ""} · ${sorted.length} ${
          sorted.length === 1 ? "model" : "models"
        }`
      : null;
  // The keep-alive only matters at the moment of pressing Load; once every
  // installed model is resident there is nothing it could apply to.
  const loadable = sorted.some((row) => !resident.has(row.name));

  let body: React.ReactNode;
  if (models.isPending) {
    body = <p className="px-4 py-3 text-xs text-faint md:px-5">Loading local models…</p>;
  } else if (models.isError) {
    body = (
      <p className="px-4 py-3 text-xs text-faint md:px-5">
        We couldn&apos;t reach Ollama at{" "}
        <span className="font-mono text-dim">{provider.base_url ?? "its default address"}</span>.
        Check the host is up and the base URL is right, then try again.{" "}
        <button
          type="button"
          onClick={() => void models.refetch()}
          className="font-medium text-ink underline"
        >
          Retry
        </button>
      </p>
    );
  } else if (sorted.length === 0) {
    body = models.data?.detail ? (
      <p className="px-4 py-3 text-xs text-faint md:px-5">
        Couldn&apos;t list models — {models.data.detail}
      </p>
    ) : (
      <p className="px-4 py-3 text-xs text-faint md:px-5">
        No models on this Ollama host yet. Pull one on the host (for example{" "}
        <span className="font-mono text-dim">ollama pull qwen3</span>) and it shows up here.
      </p>
    );
  } else {
    body = (
      <>
        {loaded.data?.detail ? (
          <p className="border-b border-line px-4 py-2 text-xs text-warn md:px-5">
            Couldn&apos;t check what&apos;s loaded — {loaded.data.detail}
          </p>
        ) : null}
        <ul className="divide-y divide-line">
          {sorted.map((model) => {
            const facts = resident.get(model.name);
            const loading = pending.has(model.name);
            const isUnloading = unloading.has(model.name);
            const capabilities = capabilityLabels(model.capabilities);
            const usedAs = ollamaUsedAsText(profiles, provider.id, model.name);
            return (
              <li
                key={model.name}
                data-testid={`ollama-model-${model.name}`}
                className="flex flex-wrap items-start gap-x-4 gap-y-1 px-4 py-3 md:px-5"
              >
                {/* basis-56: on a phone the buttons drop under the facts
                    instead of squeezing a model name into a one-word-per-
                    line column. */}
                <div className="min-w-0 flex-1 basis-56 space-y-0.5">
                  <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="min-w-0 break-words font-mono text-sm text-ink">
                      {model.name}
                    </span>
                    {facts ? <Badge tone="ok">Loaded</Badge> : null}
                    {usedAs ? <span className="text-xs text-faint">{usedAs}</span> : null}
                  </p>
                  {/* The live fact is the only text-dim on the row; the
                      static facts stay faint. */}
                  <p className="text-xs">
                    {facts ? (
                      <>
                        <span className="text-dim">
                          {`${ollamaMemoryText(facts)} · ${ollamaLeaseText(facts)}`}
                        </span>
                        <span className="text-faint"> · </span>
                      </>
                    ) : null}
                    <span className="text-faint">{metaLine(model)}</span>
                    {capabilities.map((label) => (
                      <Fragment key={label}>
                        <span className="text-faint"> · </span>
                        <span className="text-faint">{label}</span>
                      </Fragment>
                    ))}
                  </p>
                  {loading ? (
                    <p role="status" className="flex items-center gap-2 text-xs text-dim">
                      <span
                        aria-hidden
                        className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent"
                      />
                      Loading {ollamaDisplayName(model.name)} — {formatModelSize(model.size_bytes)},
                      this can take a minute or more.
                    </p>
                  ) : null}
                  {rowErrors[model.name] ? (
                    <p role="alert" className="text-xs text-danger">
                      {rowErrors[model.name]}
                    </p>
                  ) : null}
                </div>
                {isAdmin ? (
                  <div className="flex shrink-0 flex-wrap items-center gap-1">
                    {facts ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={isUnloading}
                        onClick={() => requestUnload(model.name)}
                      >
                        {isUnloading ? "Unloading…" : "Unload"}
                      </Button>
                    ) : (
                      <Button size="sm" disabled={loading} onClick={() => requestLoad(model.name)}>
                        {loading ? "Loading…" : "Load"}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => onUseAsModel(profilePrefillForOllamaModel(provider.id, model))}
                    >
                      Use as model
                    </Button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      </>
    );
  }

  return (
    <section
      data-testid="ollama-panel"
      aria-label={`Local models on ${provider.display_name}`}
      className="rounded-2xl border border-line bg-surface"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-3 md:px-5">
        <h3 className="flex items-center gap-1.5 font-display text-sm font-semibold text-ink">
          <HardDrive size={14} aria-hidden /> {provider.display_name}
        </h3>
        {caption ? <span className="text-xs text-faint">{caption}</span> : null}
        <OllamaHeaderStatus host={host} />
        {isAdmin && loadable ? (
          <label className="flex basis-full items-center gap-1.5 text-xs text-faint md:basis-auto">
            Keep loaded for{" "}
            <select
              aria-label="Keep loaded for"
              title="How long a loaded model stays in memory after its last request"
              className="h-10 rounded-md border border-line bg-surface px-1.5 text-base text-ink md:h-8 md:text-xs"
              value={keepAlive}
              onChange={(event) => setKeepAlive(event.target.value as OllamaKeepAlive)}
            >
              {OLLAMA_KEEP_ALIVE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
      {body}
    </section>
  );
}
