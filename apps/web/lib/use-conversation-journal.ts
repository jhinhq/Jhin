"use client";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { consumeEventFrames, mergeConversationItems, type ConversationItem, type ConversationItemsPage } from "@/lib/agentic-chat";

type JournalStatus = "connecting" | "connected" | "reconnecting" | "polling";
interface JournalState {
  key: string; items: ConversationItem[]; status: JournalStatus; legacyFallback: boolean;
  nextBefore: number | null; loadingEarlier: boolean; historyError: string | null;
}
interface JournalSession { state: JournalState; cursor: number; initialized: boolean; stopped: boolean; }
const emptyState = (key: string): JournalState => ({ key, items: [], status: "connecting", legacyFallback: false, nextBefore: null, loadingEarlier: false, historyError: null });

export function useConversationJournal(workspaceId: string, conversationId: string | null, enabled = true) {
  const queryClient = useQueryClient();
  const key = `${workspaceId}:${conversationId}:${enabled}`;
  const [stored, setStored] = useState<JournalState>(() => emptyState(key));
  const state = stored.key === key ? stored : emptyState(key);
  const active = useRef<JournalSession | null>(null);
  const path = `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`;
  const publish = useCallback((session: JournalSession, update: Partial<JournalState>) => {
    if (session.stopped || active.current !== session) return;
    session.state = { ...session.state, ...update };
    setStored(session.state);
  }, []);
  const invalidate = useCallback(() => {
    for (const name of ["conversation", "conversation-messages", "conversation-tool-calls", "conversation-activity", "conversation-files", "conversation-runtime"]) void queryClient.invalidateQueries({ queryKey: [name, workspaceId, conversationId] });
  }, [queryClient, workspaceId, conversationId]);
  useEffect(() => {
    if (!conversationId || !enabled) return;
    const session: JournalSession = { state: emptyState(key), cursor: 0, initialized: false, stopped: false };
    active.current = session;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    let failures = 0, recoveryRequired = false;
    const snapshot = async (recover = false) => {
      const loadedIds = session.state.items.map((item) => item.id);
      const result = await api<ConversationItemsPage>(`${path}/items`, { signal: controller.signal });
      let refreshed = result.items;
      // A latest-page snapshot alone cannot refresh old loaded actions. Query
      // their identities in bounded batches, without walking unloaded history.
      for (let offset = 0; offset < loadedIds.length; offset += 100) {
        const query = new URLSearchParams();
        for (const id of loadedIds.slice(offset, offset + 100)) query.append("item_id", id);
        const page = await api<ConversationItemsPage>(`${path}/items?${query}`, { signal: controller.signal });
        refreshed = mergeConversationItems(refreshed, page.items);
      }
      if (session.stopped) return;
      publish(session, {
        items: recover ? refreshed : mergeConversationItems(session.state.items, refreshed),
        ...(!session.initialized || recover ? { nextBefore: result.next_before } : {}),
      });
      // Polling does not acknowledge journal events. Only a complete initial or
      // explicitly required recovery snapshot may replace the replay cursor.
      if (!session.initialized || recover) session.cursor = result.cursor;
      session.initialized = true;
    };
    const connect = async () => {
      try {
        if (!session.initialized || recoveryRequired || failures >= 3) {
          await snapshot(recoveryRequired);
          recoveryRequired = false;
        }
        if (session.stopped) return;
        const response = await fetch(`${path}/events?after=${session.cursor}`, { signal: controller.signal, headers: { accept: "text/event-stream" }, cache: "no-store" });
        if (!response.ok || !response.body || !response.headers.get("content-type")?.includes("text/event-stream")) throw new Error("Stream unavailable");
        publish(session, { status: "connected" }); failures = 0;
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = "";
        try {
          while (!session.stopped && !recoveryRequired) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            if (buffer.length > 2_000_000) throw new Error("Event frame too large");
            const parsed = consumeEventFrames(buffer); buffer = parsed.rest;
            for (const event of parsed.events) {
              if (event.event === "snapshot_required") { recoveryRequired = true; invalidate(); break; }
              if (event.event !== "item") continue;
              const item = JSON.parse(event.data) as ConversationItem;
              if (item.version !== 1 || typeof item.id !== "string" || !Number.isSafeInteger(item.sequence)) continue;
              session.cursor = Math.max(session.cursor, item.sequence, Number(event.id) || 0);
              publish(session, { items: mergeConversationItems(session.state.items, [item]) });
              if (item.kind !== "generation") invalidate();
            }
          }
        } finally { try { await reader.cancel(); } catch { /* An aborted transport is already closed. */ } reader.releaseLock(); }
        if (!session.stopped) throw new Error("Stream closed");
      } catch (error) {
        if (session.stopped) return;
        if (error instanceof ApiError && (error.status === 404 || error.status === 403)) { publish(session, { status: "polling", legacyFallback: !session.initialized }); return; }
        failures += 1;
        publish(session, { status: failures >= 3 ? "polling" : "reconnecting" });
        invalidate();
        reconnect = setTimeout(() => { void connect(); }, recoveryRequired ? 0 : Math.min(30_000, 1000 * 2 ** Math.min(failures, 5)));
      }
    };
    void connect();
    return () => { session.stopped = true; controller.abort(); if (reconnect) clearTimeout(reconnect); };
  }, [conversationId, enabled, invalidate, key, path, publish]);
  const loadEarlier = async () => {
    const session = active.current;
    if (!enabled || !session || session.state.key !== key || session.stopped || session.state.nextBefore === null || session.state.loadingEarlier) return;
    publish(session, { loadingEarlier: true, historyError: null });
    try {
      const page = await api<ConversationItemsPage>(`${path}/items`, { params: { before: session.state.nextBefore } });
      publish(session, { items: mergeConversationItems(session.state.items, page.items), nextBefore: page.next_before });
    } catch { publish(session, { historyError: "Couldn't load earlier activity. Try again." }); }
    finally { publish(session, { loadingEarlier: false }); }
  };
  return { items: state.items, status: state.status, legacyFallback: state.legacyFallback, loadEarlier, hasEarlier: state.nextBefore !== null, loadingEarlier: state.loadingEarlier, historyError: state.historyError };
}
