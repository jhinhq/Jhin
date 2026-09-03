"use client";

/** Whether one profile's model is in its Ollama host's memory, shown where
 * models are chosen — the profile card and the default hero — so nobody has
 * to go to the provider card to learn that the model they are about to use
 * will spend its first minute reading 18 GB off disk. When it is not
 * resident, an admin can load it from right here with the same request and
 * the same default lease the panel uses. */

import { useState } from "react";
import { Badge, Button, ErrorNote } from "@/components/ui";
import { errorText } from "@/lib/api";
import { useCountdownTick, type OllamaHost } from "@/lib/ollama-host";
import {
  formatModelSize,
  OLLAMA_DEFAULT_KEEP_ALIVE,
  ollamaCanonicalName,
  ollamaLeaseText,
} from "@/lib/models";

export function OllamaLoadState({
  host,
  modelName,
  isAdmin,
}: {
  /** The page's subscription to the profile's host — see lib/ollama-host.ts. */
  host: OllamaHost;
  /** The profile's `model_name`, with or without its tag. */
  modelName: string;
  isAdmin: boolean;
}) {
  // The host files "qwen3.8" as "qwen3.8:latest"; look it up the way the
  // host reports it or a hand-typed profile never shows as loaded.
  const name = ollamaCanonicalName(modelName);
  const resident = host.resident.get(name);
  const pending = host.pending.has(name);
  const listing = host.models.data;
  const installed = listing?.models.find((row) => row.name === name);
  // Only a listing that answered cleanly can say a model is absent; an
  // unreachable host answers an empty list with a reason, which says nothing
  // about the model.
  const missing = listing !== undefined && listing.detail === null && installed === undefined;
  const [error, setError] = useState<string | null>(null);

  // "for 4 more minutes" must count down between polls, but only a resident
  // model with an expiry needs the clock running.
  useCountdownTick(resident !== undefined && !resident.keepsLoaded && resident.expiresAt !== null);

  const requestLoad = () => {
    setError(null);
    host.load(name, OLLAMA_DEFAULT_KEEP_ALIVE).then(
      (result) => {
        if (!result.ok) setError(result.detail);
      },
      (failure: unknown) => setError(errorText(failure, "Loading the model failed.")),
    );
  };

  let line: React.ReactNode;
  if (resident) {
    line = (
      <>
        <Badge tone="ok">Loaded</Badge>
        <span className="text-xs text-faint">{ollamaLeaseText(resident)}</span>
      </>
    );
  } else if (pending) {
    line = (
      <p role="status" className="flex items-center gap-2 text-xs text-dim">
        <span
          aria-hidden
          className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent"
        />
        Loading{installed ? ` — ${formatModelSize(installed.size_bytes)}` : "…"}
      </p>
    );
  } else if (missing) {
    line = <span className="text-xs text-faint">Not installed on the host</span>;
  } else if (isAdmin) {
    line = (
      <>
        <span className="text-xs text-faint">Not loaded</span>
        <Button size="sm" variant="ghost" onClick={requestLoad}>
          Load
        </Button>
      </>
    );
  } else {
    line = <span className="text-xs text-faint">Not loaded</span>;
  }

  return (
    <div data-testid="ollama-load-state" className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">{line}</div>
      <ErrorNote message={error} />
    </div>
  );
}
