"use client";

/** One live picture of each Ollama host on the Models page.
 *
 * Several cards describe the same host — the provider card's panel and its
 * header line, every profile card that runs on it, the default hero — and
 * each wants to know what is resident right now. Subscribing in each card
 * would mean as many ten-second polls of the same endpoint, so the page
 * subscribes once per provider here and hands the result down. The set of
 * loads in progress lives here for the same reason: a Load pressed on a
 * profile card must read as loading on the panel's row too, and the other
 * way round, or the two disagree for the minute a cold load takes. */

import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ollamaLoadedQuery, ollamaModelsQuery, useInvalidateOllama } from "@/lib/hooks";
import { residentByName, type OllamaResident } from "@/lib/models";
import type { OllamaKeepAlive, OllamaLoaded, OllamaLoadResult, OllamaModels } from "@/lib/types";

export interface OllamaHost {
  providerId: string;
  /** The installed listing: fetched once, refreshed after a load or unload. */
  models: UseQueryResult<OllamaModels>;
  /** What is resident: polled every ten seconds. */
  loaded: UseQueryResult<OllamaLoaded>;
  /** Resident models by name — the poll's answer once it has one, the
   * listing's snapshot before. */
  resident: ReadonlyMap<string, OllamaResident>;
  /** Models whose load was requested and the poll has not yet confirmed.
   * The API can answer "loading" before the host has finished, so its
   * reply alone never ends the wait. */
  pending: ReadonlySet<string>;
  /** Models whose unload request is still in flight. */
  unloading: ReadonlySet<string>;
  /** Ask the host to read a model into memory for `keepAlive`. Resolves
   * with the API's answer — `ok: false` is the host's own refusal, worth
   * showing in its words — and rejects when the request itself failed. */
  load: (model: string, keepAlive: OllamaKeepAlive) => Promise<OllamaLoadResult>;
  unload: (model: string) => Promise<OllamaLoadResult>;
}

/** Provider and model in one string, so one list can hold every host's
 * in-progress rows. A newline can appear in neither half. */
function rowKey(providerId: string, model: string): string {
  return `${providerId}\n${model}`;
}

function rowsOf(keys: readonly string[], providerId: string): Set<string> {
  const prefix = `${providerId}\n`;
  return new Set(
    keys.filter((key) => key.startsWith(prefix)).map((key) => key.slice(prefix.length)),
  );
}

export function useOllamaHosts(
  workspaceId: string,
  providerIds: readonly string[],
): ReadonlyMap<string, OllamaHost> {
  const models = useQueries({
    queries: providerIds.map((providerId) => ollamaModelsQuery(workspaceId, providerId)),
  });
  const loaded = useQueries({
    queries: providerIds.map((providerId) => ollamaLoadedQuery(workspaceId, providerId)),
  });
  const invalidate = useInvalidateOllama(workspaceId);
  const [pending, setPending] = useState<readonly string[]>([]);
  const [unloading, setUnloading] = useState<readonly string[]>([]);

  const base = (providerId: string) =>
    `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/ollama`;

  const load = async (
    providerId: string,
    model: string,
    keepAlive: OllamaKeepAlive,
  ): Promise<OllamaLoadResult> => {
    const key = rowKey(providerId, model);
    setPending((current) => (current.includes(key) ? current : [...current, key]));
    try {
      const result = await api<OllamaLoadResult>(`${base(providerId)}/load`, {
        method: "POST",
        body: { model, keep_alive: keepAlive },
      });
      // A hand-off is a success the poll confirms; refreshing now means it
      // starts confirming at once rather than at the next ten-second tick.
      if (result.ok) invalidate();
      else setPending((current) => current.filter((row) => row !== key));
      return result;
    } catch (error) {
      setPending((current) => current.filter((row) => row !== key));
      throw error;
    }
  };

  const unload = async (providerId: string, model: string): Promise<OllamaLoadResult> => {
    const key = rowKey(providerId, model);
    setUnloading((current) => [...current, key]);
    try {
      const result = await api<OllamaLoadResult>(`${base(providerId)}/unload`, {
        method: "POST",
        body: { model },
      });
      if (result.ok) invalidate();
      return result;
    } finally {
      setUnloading((current) => current.filter((row) => row !== key));
    }
  };

  const hosts = new Map<string, OllamaHost>();
  const landed: string[] = [];
  providerIds.forEach((providerId, index) => {
    const resident = residentByName(models[index].data?.models, loaded[index].data?.models);
    const pendingHere = rowsOf(pending, providerId);
    for (const model of pendingHere) {
      if (resident.has(model)) landed.push(rowKey(providerId, model));
    }
    hosts.set(providerId, {
      providerId,
      models: models[index],
      loaded: loaded[index],
      resident,
      pending: pendingHere,
      unloading: rowsOf(unloading, providerId),
      load: (model, keepAlive) => load(providerId, model, keepAlive),
      unload: (model) => unload(providerId, model),
    });
  });
  // A pending load the poll now reports resident is done. Clearing it during
  // render rather than in an effect means no row ever paints "Loading…"
  // beside a "Loaded" badge for one frame.
  if (landed.length > 0) {
    setPending((current) => current.filter((row) => !landed.includes(row)));
  }
  return hosts;
}

/** Re-render every five seconds while `active`, so a lease shown as "for 4
 * more minutes" counts down between the ten-second polls instead of jumping.
 * Only a card with an expiry to show pays for the clock. */
export function useCountdownTick(active: boolean): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setTick((count) => count + 1), 5_000);
    return () => clearInterval(timer);
  }, [active]);
}
