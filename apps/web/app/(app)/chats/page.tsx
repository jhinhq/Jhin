"use client";

/** Chats home: pick an agent, say what you need, and a new chat starts. */

import { useTransientMutation } from "@/lib/use-transient-mutation";
import { mayContainSecret, type SecureChatInput } from "@/lib/private-input";
import { SecureInputButton } from "@/components/chat/secure-input";
import { Sparkles } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useRef, useState } from "react";
import { AgentPicker } from "@/components/chat/agent-picker";
import { Composer, type ComposerHandle } from "@/components/chat/composer";
import { FirstRunSteps } from "@/components/first-run-steps";
import { LogoMark } from "@/components/brand/logo-mark";
import { EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { LAST_AGENT_STORAGE_KEY, STARTER_PROMPTS, newTurn, stashCarriedDraft } from "@/lib/chat";
import { useAgents, useInvalidateConversations, useModelProfiles } from "@/lib/hooks";
import { AGENTIC_WORKSPACE_ENABLED } from "@/lib/agentic-features";
import { emptyDraft, saveDraft, type ExecutionMode } from "@/lib/agentic-chat";
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

type CreateChatBody = { agent_id: string; text?: string; client_turn_id?: string; execution_mode?: ExecutionMode; model_profile_id?: string; secure_inputs?: SecureChatInput[] };

function ChatsHome() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Profile "Chat" buttons and directory cards deep-link with ?agent=<id>.
  const requestedAgentId = searchParams.get("agent");
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agents = useAgents(workspaceId);
  const profiles = useModelProfiles(workspaceId);
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("act"), [profileId, setProfileId] = useState("");
  const invalidate = useInvalidateConversations(workspaceId);
  const [text, setText] = useState("");
  // Mirrors `text` so the mutation callbacks read what is in the box now, not
  // what it held when the request was sent.
  const textRef = useRef("");
  const changeText = useCallback((value: string) => {
    textRef.current = value;
    setText(value);
  }, []);
  const composerRef = useRef<ComposerHandle>(null);
  const [chosenAgentId, setChosenAgentId] = useState<string | null>(null);
  const [rememberedId] = useState<string | null>(() =>
    typeof window === "undefined" ? null : readLastAgent(),
  );
  const [error, setError] = useState<string | null>(null);
  const [pendingCreate, setPendingCreate] = useState<CreateChatBody | null>(null);

  const activeAgents = useMemo<Agent[]>(
    () => (agents.data ?? []).filter((agent) => agent.status === "active"),
    [agents.data],
  );

  // Selection: an explicit pick, then the deep-linked agent, then the
  // remembered one, then the first active agent.
  const agentId = useMemo(() => {
    const isActive = (id: string | null) => Boolean(id && activeAgents.some((agent) => agent.id === id));
    if (isActive(chosenAgentId)) return chosenAgentId;
    if (isActive(requestedAgentId)) return requestedAgentId;
    if (isActive(rememberedId)) return rememberedId;
    return activeAgents[0]?.id ?? null;
  }, [chosenAgentId, requestedAgentId, rememberedId, activeAgents]);

  const create = useTransientMutation({
    mutationFn: (body: CreateChatBody) =>
      api<ConversationDetail>(`/api/v1/workspaces/${workspaceId}/conversations`, {
        method: "POST",
        body,
      }),
    onMutate: (body) => { setError(null); setPendingCreate(body); },
    onSuccess: (detail, variables) => {
      setError(null);
      setPendingCreate(null);
      rememberAgent(detail.conversation.primary_agent_id ?? agentId ?? "");
      // Anything typed while the first turn was in flight would be lost with
      // this page; hand it to the conversation we are about to open.
      stashCarriedDraft(detail.conversation.id, textRef.current);
      if (!variables.text) saveDraft(workspaceId, detail.conversation.id, { ...emptyDraft(), text: textRef.current, execution_mode: variables.execution_mode ?? "act", model_profile_id: variables.model_profile_id });
      invalidate();
      router.push(`/chats/${detail.conversation.id}`);
    },
    onError: (err, variables) => {
      setText((current) => current || (variables.text && !variables.secure_inputs?.length && !mayContainSecret(variables.text) ? variables.text : ""));
      setError(
        err instanceof ApiError && !variables.secure_inputs?.length && !mayContainSecret(variables.text ?? "")
          ? `Couldn't start the chat: ${err.detail}`
          : "Couldn't start the chat. Check your connection and try again.",
      );
    },
  });

  const send = (value: string, secureInputs?: SecureChatInput[]) => {
    if (!agentId) return;
    // Clear straight away so the box is usable during the redirect, exactly
    // as it behaves once the chat exists.
    changeText("");
    const body: CreateChatBody = { agent_id: agentId, ...newTurn(value || "Use this credential for the requested setup."), ...(secureInputs ? { secure_inputs: secureInputs } : {}), ...(AGENTIC_WORKSPACE_ENABLED ? { execution_mode: executionMode, model_profile_id: profileId || undefined } : {}) };
    const signature = (candidate: CreateChatBody) => JSON.stringify({...candidate,client_turn_id:undefined});
    create.mutate(pendingCreate && signature(pendingCreate)===signature(body) ? pendingCreate : body);
  };

  const selectedAgent = activeAgents.find((agent) => agent.id === agentId) ?? null;
  const firstName = user.display_name.split(/\s+/)[0] || "there";
  const canStart = can("member");

  return (
    // AppShell already renders the page's <main id="main">; this is a plain
    // scroll container inside it.
    <div className="min-h-0 flex-1 overflow-y-auto">
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
            description="Two quick steps and your first chat is ready."
            action={<FirstRunSteps workspaceId={workspaceId} isAdmin={can("admin")} />}
          />
        ) : (
          <div className="space-y-6">
            <Composer
              ref={composerRef}
              variant="large"
              autoFocus
              value={text}
              onChange={changeText}
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
              controls={AGENTIC_WORKSPACE_ENABLED ? <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1"><select aria-label="First turn's mode" value={executionMode} onChange={(event) => setExecutionMode(event.target.value as ExecutionMode)} disabled={create.isPending} className="h-9 rounded-lg bg-transparent p-1 text-xs text-dim"><option value="ask">Ask</option><option value="plan">Plan</option><option value="act">Act</option></select><select aria-label="First turn's model" value={profileId} onChange={(event) => setProfileId(event.target.value)} disabled={create.isPending} className="h-9 min-w-0 max-w-40 rounded-lg bg-transparent p-1 text-xs text-dim"><option value="">Agent’s model</option>{profiles.data?.map((profile) => <option key={profile.id} value={profile.id}>{profile.display_name}</option>)}</select><button type="button" disabled={!canStart || !agentId || create.isPending} className="min-h-9 rounded-lg px-2 text-xs text-accent-strong hover:bg-hover disabled:opacity-40" onClick={() => { if (agentId) create.mutate({ agent_id: agentId, execution_mode: executionMode, model_profile_id: profileId || undefined }); }}>Start with files or a project</button></div> : undefined}
            />
            <SecureInputButton disabled={!canStart || !agentId || create.isPending} onSend={(input) => send(text, [input])} />
            <ErrorNote message={error} />
            {pendingCreate && error && !create.isPending ? <button type="button" onClick={()=>create.mutate(pendingCreate)} className="min-h-10 rounded-xl border border-line px-3 text-sm text-accent-strong">Retry starting chat</button> : null}

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

            {canStart ? (
              <section className="space-y-2">
                <h2 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
                  <Sparkles size={12} aria-hidden /> Try asking
                </h2>
                <ul className="grid gap-2 sm:grid-cols-2">
                  {STARTER_PROMPTS.map((prompt) => (
                    <li key={prompt}>
                      <button
                        type="button"
                        onClick={() => {
                          changeText(prompt);
                          composerRef.current?.focus();
                        }}
                        className="min-h-[44px] w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-left text-sm text-dim transition-colors hover:border-line-strong hover:bg-hover hover:text-ink"
                      >
                        {prompt}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ChatsHomePage() {
  // useSearchParams needs a Suspense boundary for static prerendering.
  return (
    <Suspense fallback={<Spinner />}>
      <ChatsHome />
    </Suspense>
  );
}
