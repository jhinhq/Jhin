"use client";

/** The Ollama provider's stand-in for a balance block, on the provider card
 * itself. There is no bill to show for a local host, so the block answers
 * the questions an admin has instead: what is installed, what is resident
 * in memory right now, and the two things worth doing about it — load a
 * model ahead of a conversation so the first reply does not wait on 18 GB
 * of weights, and unload one to make room for another.
 *
 * It reads and acts through the page's one subscription to the host
 * (lib/ollama-host.ts) rather than querying on its own, so the profile
 * cards above it and the status line beside it always agree with its rows. */

import { HardDrive } from "lucide-react";
import { useState } from "react";
import { Badge, Button } from "@/components/ui";
import { errorText } from "@/lib/api";
import { formatTokens } from "@/lib/format";
import { useCountdownTick, type OllamaHost } from "@/lib/ollama-host";
import {
  formatModelSize,
  OLLAMA_DEFAULT_KEEP_ALIVE,
  OLLAMA_KEEP_ALIVE_OPTIONS,
  ollamaDisplayName,
  ollamaExpiryText,
  ollamaMemoryText,
  profilePrefillForOllamaModel,
  type ProfilePrefill,
} from "@/lib/models";
import type { ModelProvider, OllamaKeepAlive, OllamaModel } from "@/lib/types";

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

export function OllamaPanel({
  provider,
  host,
  isAdmin,
  onError,
  onUseAsModel,
}: {
  provider: ModelProvider;
  /** The page's subscription to this host — see lib/ollama-host.ts. */
  host: OllamaHost;
  isAdmin: boolean;
  /** Request failures go to the card's ErrorNote, beside the other actions. */
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

  // "expires in 4 minutes" must count down between polls, but only a row
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
  const loadedCount = sorted.filter((row) => resident.has(row.name)).length;
  const version = models.data?.version;
  const summary =
    sorted.length > 0
      ? `Ollama${version ? ` ${version}` : ""} — ${sorted.length} ${
          sorted.length === 1 ? "model" : "models"
        }, ${loadedCount} loaded`
      : null;

  let body: React.ReactNode;
  if (models.isPending) {
    body = <p className="text-faint">Loading local models…</p>;
  } else if (models.isError) {
    body = (
      <p className="text-faint">
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
      <p className="text-faint">Couldn&apos;t list models — {models.data.detail}</p>
    ) : (
      <p className="text-faint">
        No models on this Ollama host yet. Pull one on the host (for example{" "}
        <span className="font-mono text-dim">ollama pull qwen3</span>) and it shows up here.
      </p>
    );
  } else {
    body = (
      <>
        {loaded.data?.detail ? (
          <p className="text-warn">
            Couldn&apos;t check what&apos;s loaded — {loaded.data.detail}
          </p>
        ) : null}
        {isAdmin ? (
          <label className="flex flex-wrap items-center gap-1.5 text-faint">
            Keep loaded for
            <select
              aria-label="Keep loaded for"
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
            after its last request
          </label>
        ) : null}
        <ul className="divide-y divide-line">
          {sorted.map((model) => {
            const facts = resident.get(model.name);
            const loading = pending.has(model.name);
            const isUnloading = unloading.has(model.name);
            const capabilities = capabilityLabels(model.capabilities);
            return (
              <li
                key={model.name}
                data-testid={`ollama-model-${model.name}`}
                className="space-y-1 py-2 first:pt-0 last:pb-0"
              >
                <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                  {/* basis-48: on a phone the buttons drop under the facts
                      instead of squeezing a model name into a one-word-per-
                      line column. */}
                  <div className="min-w-0 flex-1 basis-48 space-y-0.5">
                    <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="min-w-0 break-words font-mono text-ink">{model.name}</span>
                      {facts ? <Badge tone="ok">Loaded</Badge> : null}
                      {capabilities.map((label) => (
                        <span
                          key={label}
                          className="rounded-full border border-line px-1.5 text-[11px] leading-4 text-dim"
                        >
                          {label}
                        </span>
                      ))}
                    </p>
                    <p className="text-faint">{metaLine(model)}</p>
                    {facts ? (
                      <p className="text-dim">
                        {ollamaMemoryText(facts)} · {ollamaExpiryText(facts)}
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
                        <Button
                          size="sm"
                          disabled={loading}
                          onClick={() => requestLoad(model.name)}
                        >
                          {loading ? "Loading…" : "Load"}
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          onUseAsModel(profilePrefillForOllamaModel(provider.id, model))
                        }
                      >
                        Use as model
                      </Button>
                    </div>
                  ) : null}
                </div>
                {loading ? (
                  <p role="status" className="flex items-center gap-2 text-dim">
                    <span
                      aria-hidden
                      className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent"
                    />
                    Loading {ollamaDisplayName(model.name)} — {formatModelSize(model.size_bytes)},
                    this can take a minute or more.
                  </p>
                ) : null}
                {rowErrors[model.name] ? (
                  <p role="alert" className="text-danger">
                    {rowErrors[model.name]}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      </>
    );
  }

  return (
    <div data-testid="ollama-panel" className="space-y-2 rounded-xl bg-raised px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <p className="flex items-center gap-1.5 font-medium text-ink">
          <HardDrive size={12} aria-hidden /> Local models
        </p>
        {summary ? <p className="text-faint">{summary}</p> : null}
      </div>
      {body}
    </div>
  );
}
