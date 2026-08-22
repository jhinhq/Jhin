"use client";

/** Chats home: pick an agent, say what you need, and a new chat starts. */

import { useMutation } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { AgentPicker } from "@/components/chat/agent-picker";
import { Composer } from "@/components/chat/composer";
import { LogoMark } from "@/components/brand/logo-mark";
import { Button, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { LAST_AGENT_STORAGE_KEY, STARTER_PROMPTS, newTurn } from "@/lib/chat";
import { useAgents, useInvalidateConversations } from "@/lib/hooks";
import type { Agent, ConversationDetail } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function readLastAgent(): string | null {
  try {
    return window.localStorage.getItem(LAST_AGENT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function rememberAgent(id: string) {
  try {
    window.localStorage.setItem(LAST_AGENT_STORAGE_KEY, id);
  } catch {
    // Private mode or quota: remembering the agent is a nicety, not a requirement.
  }
}

export default function ChatsHomePage() {
  const router = useRouter();
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agents = useAgents(workspaceId);
  const invalidate = useInvalidateConversations(workspaceId);
  const [text, setText] = useState("");
  const [chosenAgentId, setChosenAgentId] = useState<string | null>(null);
  const [rememberedId] = useState<string | null>(() =>
    typeof window === "undefined" ? null : readLastAgent(),
  );
  const [error, setError] = useState<string | null>(null);

  const activeAgents = useMemo<Agent[]>(
    () => (agents.data ?? []).filter((agent) => agent.status === "active"),
    [agents.data],
  );

  // Selection falls back to the remembered agent, then the first active one.
  const agentId = useMemo(() => {
    if (chosenAgentId && activeAgents.some((agent) => agent.id === chosenAgentId)) return chosenAgentId;
    if (rememberedId && activeAgents.some((agent) => agent.id === rememberedId)) return rememberedId;
    return activeAgents[0]?.id ?? null;
  }, [chosenAgentId, rememberedId, activeAgents]);

  const create = useMutation({
    mutationFn: (body: { agent_id: string; text: string; client_turn_id: string }) =>
      api<ConversationDetail>(`/api/v1/workspaces/${workspaceId}/conversations`, {
        method: "POST",
        body,
      }),
    onSuccess: (detail) => {
      setError(null);
      rememberAgent(detail.conversation.primary_agent_id ?? agentId ?? "");
      invalidate();
      router.push(`/chats/${detail.conversation.id}`);
    },
    onError: (err) =>
      setError(
        err instanceof ApiError
          ? `Couldn't start the chat: ${err.detail}`
          : "Couldn't start the chat. Check your connection and try again.",
      ),
  });

  const send = (value: string) => {
    if (!agentId) return;
    create.mutate({ agent_id: agentId, ...newTurn(value) });
  };

  const selectedAgent = activeAgents.find((agent) => agent.id === agentId) ?? null;
  const firstName = user.display_name.split(/\s+/)[0] || "there";
  const canStart = can("member");

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-5 py-10 sm:px-8">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <LogoMark className="h-12 w-auto" />
          <h1 className="font-display text-2xl font-semibold text-ink sm:text-3xl">
            Hi {firstName}, what would you like to get done?
          </h1>
          <p className="max-w-md text-sm text-dim">
            Pick an agent and describe the outcome you want. They&apos;ll keep you posted here.
          </p>
        </div>

        {agents.isPending ? (
          <div className="flex justify-center py-6">
            <Spinner label="Finding your agents…" />
          </div>
        ) : agents.isError ? (
          <ErrorNote
            message={
              agents.error instanceof ApiError
                ? `Couldn't load your agents (${agents.error.detail}). Refresh to try again.`
                : "Couldn't load your agents. Check your connection and refresh to try again."
            }
          />
        ) : activeAgents.length === 0 ? (
          <EmptyState
            title="No agents yet"
            description="Create your first agent to start a chat. It only takes a minute."
            action={
              can("admin") ? (
                <Link href="/agents/new">
                  <Button variant="primary">Create an agent</Button>
                </Link>
              ) : (
                <p className="text-xs text-faint">Ask a workspace admin to add one.</p>
              )
            }
          />
        ) : (
          <div className="space-y-6">
            <Composer
              variant="large"
              autoFocus
              value={text}
              onChange={setText}
              onSend={send}
              sending={create.isPending}
              disabled={!canStart || !agentId}
              disabledReason={
                !canStart
                  ? "Viewers can read chats but can't start them. Ask an admin for member access."
                  : "Choose an agent to get started."
              }
              placeholder={
                selectedAgent ? `Ask ${selectedAgent.name} to…` : "What would you like to get done?"
              }
              hint={
                selectedAgent
                  ? `Talking to ${selectedAgent.name}${selectedAgent.role_title ? `, ${selectedAgent.role_title}` : ""}. Enter to send · Shift+Enter for a new line`
                  : null
              }
            />
            <ErrorNote message={error} />

            <section className="space-y-2">
              <h2 className="text-[11px] font-medium uppercase tracking-wider text-faint">
                Talk to
              </h2>
              <AgentPicker
                agents={activeAgents}
                selectedId={agentId}
                onSelect={(id) => {
                  setChosenAgentId(id);
                  rememberAgent(id);
                }}
              />
            </section>

            <section className="space-y-2">
              <h2 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
                <Sparkles size={12} aria-hidden /> Try asking
              </h2>
              <ul className="grid gap-2 sm:grid-cols-2">
                {STARTER_PROMPTS.map((prompt) => (
                  <li key={prompt}>
                    <button
                      type="button"
                      onClick={() => setText(prompt)}
                      className="min-h-[44px] w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-left text-sm text-dim transition-colors hover:border-line-strong hover:bg-hover hover:text-ink"
                    >
                      {prompt}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}
      </div>
    </main>
  );
}
