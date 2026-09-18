"use client";

/** One chat thread: header, transcript, composer, and the Details panel. */

import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ChatHeader } from "@/components/chat/chat-header";
import { Composer, type ComposerHandle } from "@/components/chat/composer";
import { TurnControls } from "@/components/chat/turn-controls";
import { ChatComposerControls } from "@/components/chat/composer-controls";
import { AGENTIC_WORKSPACE_ENABLED } from "@/lib/agentic-features";
import { AttachmentTray, useAttachments } from "@/components/chat/attachments";
import { WorkQueue } from "@/components/chat/work-queue";
import { emptyDraft, messageInputDraft, readDraft, saveDraft, type ActivityDetail, type ChatDraft } from "@/lib/agentic-chat";
import { projectChatTimeline } from "@/lib/chat-journal-projection";
import { useConversationJournal } from "@/lib/use-conversation-journal";
import { useConversationFiles, type ManagedFile } from "@/lib/workspace-files";
import { ContextPanel } from "@/components/chat/context-panel";
import { Transcript } from "@/components/chat/transcript";
import { ButtonLink, ConfirmDialog, Dialog, ErrorNote, Spinner } from "@/components/ui";
import { useSegmentAfter } from "@/lib/use-route-segment";
import { api, ApiError } from "@/lib/api";
import {
  CHAT_DETAILED_STORAGE_KEY,
  composerHintFor,
  newTurn,
  statusLabelFor,
  takeCarriedDraft,
} from "@/lib/chat";
import {
  useAnswerQuestion,
  useConversation,
  useConversationActivity,
  useConversationToolCalls,
  useConversationMessages,
  useInvalidateApprovals,
  useInvalidateConversations,
  useAgentAvatarMap,
} from "@/lib/hooks";
import type {
  AnswerQuestionIn,
  ConversationMessage,
  ConversationUpdate,
  ResumeOut,
  TurnOut,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";
import { useTransientMutation } from "@/lib/use-transient-mutation";
import { mayContainSecret, type SecureChatInput } from "@/lib/private-input";
import { SecureInputButton } from "@/components/chat/secure-input";

/** Keep polling briefly after a send so the new task shows up. */
const RECENT_SEND_WINDOW_MS = 20_000;
const WorkspacePane = lazy(() => import("@/components/chat/workspace-pane").then((module) => ({ default: module.WorkspacePane })));
type SendTurnBody = { text: string; client_turn_id: string; execution_mode?: ChatDraft["execution_mode"]; delivery?: ChatDraft["delivery"]; model_profile_id?: string; attachment_ids?: string[]; context_refs?: Record<string, unknown>[]; secure_inputs?: SecureChatInput[] };
function privateTurn(body: SendTurnBody) { return !!body.secure_inputs?.length || mayContainSecret(body.text); }
function readPendingSend(key: string): SendTurnBody | null { try { const body = JSON.parse(localStorage.getItem(key) ?? "null"); if (body && typeof body.text === "string" && privateTurn(body)) { localStorage.removeItem(key); return null; } return body && typeof body.client_turn_id === "string" && typeof body.text === "string" ? body : null; } catch { return null; } }

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function describeError(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

function ChatThread({ conversationId }: { conversationId: string }) {
  const router = useRouter();
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const avatars = useAgentAvatarMap(workspaceId);
  const queryClient = useQueryClient();
  const invalidate = useInvalidateConversations(workspaceId);
  const invalidateApprovals = useInvalidateApprovals(workspaceId);
  const wide = useMediaQuery("(min-width: 1280px)");

  // Seeded with anything typed on /chats while the first turn was redirecting
  // here. Safe as an initializer: the shell renders a spinner until identity
  // resolves on the client, so this view never renders on the server.
  const [draft, setDraft] = useState<ChatDraft>(() => { const saved = readDraft(workspaceId, conversationId); return { ...saved, text: takeCarriedDraft(conversationId) || saved.text }; });
  const text = draft.text;
  const setText = (value: string | ((current: string) => string)) => setDraft((current) => ({ ...current, text: typeof value === "function" ? value(current.text) : value }));
  const [workspaceOpen, setWorkspaceOpen] = useState(false), [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [activityDetail, setActivityDetail] = useState<ActivityDetail>(() => { try { const value = localStorage.getItem("jhin-activity-detail"); return value === "compact" || value === "detailed" ? value : "standard"; } catch { return "standard"; } });
  const journal = useConversationJournal(workspaceId, conversationId, AGENTIC_WORKSPACE_ENABLED);
  const files = useConversationFiles(workspaceId, conversationId, AGENTIC_WORKSPACE_ENABLED);
  const [uploadedFiles, setUploadedFiles] = useState<ManagedFile[]>([]);
  const selectedFiles = useQueries({ combine: (results) => results.flatMap((query) => query.data ? [query.data] : []), queries: draft.attachment_ids.slice(0, 10).filter((id) => !uploadedFiles.some((file) => file.id === id) && !files.data?.items.some((file) => file.id === id)).map((id) => ({ queryKey: ["selected-chat-file", workspaceId, id], queryFn: () => api<ManagedFile>(`/api/v1/workspaces/${workspaceId}/files/${id}`), enabled: AGENTIC_WORKSPACE_ENABLED })) });
  const allFiles = useMemo(() => [...new Map([...selectedFiles, ...uploadedFiles, ...(files.data?.items ?? [])].map((file) => [file.id, file])).values()], [selectedFiles, uploadedFiles, files.data]);
  const useFile = (file: ManagedFile, revisionId = file.current_revision_id) => {
    setDraft((current) => ({ ...current, attachment_ids: [...new Set([...current.attachment_ids, file.id])], context_refs: [...current.context_refs.filter((item) => item.id !== file.id), { kind: "file", id: file.id, version_id: revisionId, label: file.name }] }));
    setUploadedFiles((current) => [...current.filter((item) => item.id !== file.id), file]);
  };
  const attachments = useAttachments(workspaceId, conversationId, useFile);
  useEffect(() => { saveDraft(workspaceId, conversationId, draft); }, [draft, workspaceId, conversationId]);
  const [detailsOpen, setDetailsOpen] = useState(false);
  // Routine "Started working / Finished" chips are off by default; the
  // preference is remembered per browser.
  const [detailed, setDetailed] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem(CHAT_DETAILED_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleDetailed = () => {
    setDetailed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(CHAT_DETAILED_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // storage unavailable: keep the in-memory value
      }
      return next;
    });
  };
  const [sendError, setSendError] = useState<string | null>(null);
  const pendingSendKey = `jhin-pending-send:${workspaceId}:${conversationId}`;
  const [pendingSend, setPendingSend] = useState<SendTurnBody | null>(() => readPendingSend(pendingSendKey));
  const [actionError, setActionError] = useState<string | null>(null);
  // Bridges the delay between a local action and the next task-status poll.
  const [recentlySent, setRecentlySent] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const composerRef = useRef<ComposerHandle>(null);

  const detail = useConversation(workspaceId, conversationId, true);
  const conversation = detail.data?.conversation ?? null;
  const liveStatus = conversation ? statusLabelFor(conversation) : null;
  const live = liveStatus !== null || recentlySent;

  // Keep polling while a send/stop/pause/resume reaches the status endpoint.
  // Final replies are reconciled separately, even after this window expires.
  useEffect(() => {
    if (!recentlySent) return;
    const timer = window.setTimeout(() => setRecentlySent(false), RECENT_SEND_WINDOW_MS);
    return () => window.clearTimeout(timer);
  }, [recentlySent]);
  const legacyMessages = !AGENTIC_WORKSPACE_ENABLED || journal.legacyFallback;
  const messages = useConversationMessages(workspaceId, conversationId, live, legacyMessages);
  const activity = useConversationActivity(workspaceId, conversationId, live);
  const toolCalls = useConversationToolCalls(workspaceId, conversationId, live);

  // Status and transcript requests can observe different database commits.
  // When status sees a completed turn, fetch its messages before leaving the
  // transcript idle. Task revisions also catch turns that start and finish
  // between status polls; unchanged idle polls need no transcript request.
  const transcriptRevision = conversation && detail.data
    ? JSON.stringify([
        conversation.active_task_id,
        conversation.active_task_state,
        conversation.active_run_status,
        conversation.last_activity_at,
        conversation.last_message_preview,
        conversation.last_message_sender_type,
        detail.data.tasks.map((task) => [task.id, task.state, task.updated_at]),
      ])
    : null;
  useEffect(() => {
    if (conversationId === null || transcriptRevision === null) return;
    for (const resource of ["conversation-messages", "conversation-activity", "conversation-tool-calls"]) {
      const filters = { queryKey: [resource, workspaceId, conversationId], exact: true };
      // Explicit cancellation also replaces an initial request with no cached
      // data, and prevents a late pre-completion response overwriting the reply.
      void queryClient.cancelQueries(filters).then(() => queryClient.invalidateQueries(filters));
    }
  }, [conversationId, queryClient, transcriptRevision, workspaceId]);

  // Hide optimistic bubbles once the server echoes them back. Agent↔agent
  // exchanges collapse into quiet rows; date markers appear on day changes.
  const primaryAgentId = conversation?.primary_agent_id ?? null;
  const primaryAgentName = conversation?.agent_name ?? null;
  const timeline = useMemo(() => projectChatTimeline({
    messages: messages.data ?? [], tools: toolCalls.data?.items ?? [], journal: journal.items,
    optimistic: [], activity: activity.data?.items ?? [], files: allFiles, conversationId,
    primaryAgentId, primaryAgentName, detailed: AGENTIC_WORKSPACE_ENABLED ? activityDetail === "detailed" : detailed,
    agentic: AGENTIC_WORKSPACE_ENABLED,
  }), [messages.data, toolCalls.data, journal.items, activity.data, allFiles, conversationId, primaryAgentId, primaryAgentName, activityDetail, detailed]);

  const update = useMutation({
    mutationFn: (body: ConversationUpdate) =>
      api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`, {
        method: "PATCH",
        body,
      }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) =>
      setActionError(describeError(error, "Couldn't update this chat. Try again.")),
  });

  const sendTurn = useTransientMutation({
    mutationFn: (body: SendTurnBody) =>
      api<TurnOut>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/turns`, {
        method: "POST",
        body,
      }),
    onMutate: (body) => {
      setSendError(null);
      setPendingSend(body);
      try { if (privateTurn(body)) localStorage.removeItem(pendingSendKey); else localStorage.setItem(pendingSendKey, JSON.stringify(body)); } catch { /* In-memory retries still preserve identity. */ }
      setRecentlySent(true);
      // Only the server may project submitted text: intake can replace credentials.
      const previousDraft = privateTurn(body) ? { ...draft, text: "" } : draft;
      setDraft((current) => current.text && current.text.trim() !== body.text ? current : ({ ...emptyDraft(), execution_mode: current.execution_mode, model_profile_id: current.model_profile_id }));
      return { clientTurnId: body.client_turn_id, draft: previousDraft };
    },
    onSuccess: (result) => {
      setPendingSend(null);
      try { localStorage.removeItem(pendingSendKey); } catch { /* Storage unavailable. */ }
      queryClient.setQueryData<ConversationMessage[]>(
        ["conversation-messages", workspaceId, conversationId],
        (current) => {
          if (!current) return [result.message];
          return current.some((message) => message.id === result.message.id)
            ? current
            : [...current, result.message];
        },
      );
      invalidate();
    },
    onError: (error, body, context) => {
      setDraft((current) => current.text || current.attachment_ids.length ? current : context?.draft ?? current);
      setSendError(privateTurn(body) ? "Message delivery is unconfirmed. Retry delivery checks the same request without creating a second turn." : describeError(error, "Couldn't send your message. Check your connection and try again."));
      composerRef.current?.focus();
    },
  });

  // Picking a failed turn back up. The endpoint is the thing that makes this
  // safe to press twice -- a second press while the first is still working
  // answers with the same task and `created: false` -- so the button is
  // disabled only while a request is in flight, not permanently after one.
  const resume = useMutation({
    mutationFn: () =>
      api<ResumeOut>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/resume`, {
        method: "POST",
      }),
    onMutate: () => {
      setActionError(null);
      setRecentlySent(true);
    },
    onSuccess: () => {
      invalidate();
    },
    onError: (error) =>
      setActionError(
        describeError(error, "Couldn't pick that back up. Try sending the message again."),
      ),
  });

  // The honest alternative to a retry the platform will not perform: the
  // words go back where they came from and the person presses Send.
  const reuseTurn = (value: string) => {
    setActionError(null);
    setText(value);
    composerRef.current?.focus();
  };

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api(`/api/v1/workspaces/${workspaceId}/approvals/${id}/${decision}`, { method: "POST" }),
    onSuccess: () => {
      setActionError(null);
      invalidateApprovals();
      invalidate();
    },
    onError: (error) =>
      setActionError(describeError(error, "Couldn't record your decision. Try again.")),
  });

  const answerQuestion = useAnswerQuestion(workspaceId);

  // The card owns its own optimistic and error state (it is one of possibly
  // several in the transcript), so this hands the result straight back to it
  // and only takes the one action the card cannot: putting the cursor in the
  // composer when the run had already stopped waiting.
  const onAnswer = async (questionId: string, body: AnswerQuestionIn) => {
    setActionError(null);
    setRecentlySent(true);
    const result = await answerQuestion.mutateAsync({ questionId, body });
    if (!result.resumed) composerRef.current?.focus();
    return result;
  };

  const taskAction = useMutation({
    mutationFn: ({ taskId, action }: { taskId: string; action: "pause" | "resume" | "cancel" }) =>
      api(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/${action}`, { method: "POST" }),
    onSuccess: () => {
      setActionError(null);
      setRecentlySent(true);
      invalidate();
    },
    onError: (error) =>
      setActionError(describeError(error, "Couldn't change the work status. Try again.")),
    // The stop confirmation stays open (frozen via `busy`) until the request
    // settles; closing on error too keeps the note above the transcript
    // visible. A no-op for pause/resume, where it is already closed.
    onSettled: () => setConfirmStop(false),
  });
  const submitTurn = (value: string, secureInputs?: SecureChatInput[]) => {
    if (!AGENTIC_WORKSPACE_ENABLED) { sendTurn.mutate({ ...newTurn(value || "Use this credential for the requested setup."), ...(secureInputs ? { secure_inputs: secureInputs } : {}) }); return; }
    if (draft.attachment_ids.length > 10) { setSendError("A message can include up to 10 files. Remove a file before sending."); return; }
    const settings = conversation?.active_task_id && draft.delivery !== "queue" ? {} : { execution_mode: draft.execution_mode, model_profile_id: draft.model_profile_id };
    const body: SendTurnBody = { ...newTurn(value || (secureInputs ? "Use this credential for the requested setup." : "Please review the attached files.")), ...settings, delivery: draft.delivery, attachment_ids: draft.attachment_ids, context_refs: draft.context_refs.map((reference) => ({ type: reference.kind, id: reference.id, ...(reference.version_id ? { revision_id: reference.version_id } : {}), ...(reference.label ? { label: reference.label } : {}) })), ...(secureInputs ? { secure_inputs: secureInputs } : {}) };
    const signature = (candidate: SendTurnBody) => JSON.stringify({ ...candidate, client_turn_id: undefined });
    sendTurn.mutate(pendingSend && !pendingAcknowledged && signature(pendingSend) === signature(body) ? pendingSend : body);
  };
  const appendFeedback = (value: string) => { setText((current) => current.trim() ? `${current}\n\n${value}` : value); composerRef.current?.focus(); };

  const pendingAcknowledged = !!pendingSend && ((messages.data ?? []).some((message) => message.content_json.client_turn_id === pendingSend.client_turn_id) || journal.items.some((item) => item.kind === "message" && (item.data.content_json as Record<string, unknown> | undefined)?.client_turn_id === pendingSend.client_turn_id));
  useEffect(() => {
    if (!pendingAcknowledged) return;
    try { localStorage.removeItem(pendingSendKey); } catch { /* In-memory delivery state remains authoritative. */ }
    // The journal can confirm a send whose HTTP response was lost. Dispose of
    // its private retry buffer as soon as that external acknowledgement arrives.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPendingSend(null);
  }, [pendingAcknowledged, pendingSendKey]);

  const branch = useMutation({
    mutationFn: (message: ConversationMessage) => api<{ conversation_id: string }>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/branches`, { method: "POST", body: { message_id: message.id, title: `Branch: ${conversation?.title ?? "Chat"}` } }),
    onSuccess: (result) => { invalidate(); router.push(`/chats/${encodeURIComponent(result.conversation_id)}`); },
    onError: (error) => setActionError(describeError(error, "Couldn't branch this conversation.")),
  });

  if (detail.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner label="Opening chat…" />
      </div>
    );
  }

  if (!detail.data || !conversation) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-sm text-ink">
          {notFound
            ? "This chat doesn't exist or was removed."
            : `Couldn't open this chat${detail.error instanceof ApiError ? `: ${detail.error.detail}` : "."}`}
        </p>
        <ButtonLink href="/chats">Back to chats</ButtonLink>
      </div>
    );
  }

  const data = detail.data;
  const agent = data.agent;
  const agentName = agent?.name ?? conversation.agent_name ?? "Agent";
  const archived = conversation.status === "archived";
  const canWrite = can("member");
  const activeTaskId = conversation.active_task_id;

  let disabledReason: string | null = null;
  if (!canWrite) disabledReason = "Viewers can read chats but can't send messages.";
  else if (archived) disabledReason = "This chat is archived. Restore it to keep talking.";
  else if (!agent) disabledReason = "This agent is no longer in the workspace.";
  else if (agent.status === "paused") disabledReason = `${agent.name} is paused by an admin, so messages can't be sent right now.`;
  else if (agent.status === "disabled") disabledReason = `${agent.name} is turned off, so messages can't be sent right now.`;

  const composerHint = composerHintFor(liveStatus, agentName);

  const activeTaskState = conversation.active_task_state;
  const canStopTask =
    canWrite &&
    activeTaskId !== null &&
    (activeTaskState === "running" || activeTaskState === "queued" || activeTaskState === "paused");

  const panel = (
    <ContextPanel
      detail={data}
      activity={activity.data?.items ?? []}
      canAct={canWrite}
      acting={taskAction.isPending}
      onPause={() => activeTaskId && taskAction.mutate({ taskId: activeTaskId, action: "pause" })}
      onResume={() => activeTaskId && taskAction.mutate({ taskId: activeTaskId, action: "resume" })}
      onCancel={() => setConfirmStop(true)}
    />
  );

  return (
    <div className="flex min-h-0 flex-1">
      <section className="flex min-w-0 flex-1 flex-col" aria-label={conversation.title} inert={workspaceOpen && !wide ? true : undefined}>
        <ChatHeader
          conversation={conversation}
          agent={agent}
          avatar={agent ? avatars[agent.id] : null}
          canEdit={canWrite}
          detailsOpen={detailsOpen}
          onToggleDetails={() => setDetailsOpen((open) => !open)}
          detailed={detailed}
          onToggleDetailed={toggleDetailed}
          showDetailedToggle={!AGENTIC_WORKSPACE_ENABLED}
          onRename={(title) => update.mutate({ title })}
          onTogglePin={() => update.mutate({ pinned: !conversation.pinned })}
          onToggleArchive={() => update.mutate({ status: archived ? "active" : "archived" })}
          busy={update.isPending}
        />
        {AGENTIC_WORKSPACE_ENABLED ? <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-xs sm:px-8">
          <select aria-label="Activity detail" value={activityDetail} onChange={(event) => { const value = event.target.value as ActivityDetail; setActivityDetail(value); try { localStorage.setItem("jhin-activity-detail", value); } catch { /* session preference remains */ } }} className="rounded-lg bg-transparent p-1.5 text-dim"><option value="compact">Compact activity</option><option value="standard">Standard activity</option><option value="detailed">Detailed activity</option></select>
          {journal.status === "reconnecting" || detail.isError ? <span role="status" className="text-warn">Reconnecting · saved content remains visible</span> : journal.status === "polling" ? <span className="text-faint">Checking for updates</span> : null}
          {activeTaskId && canWrite ? <button type="button" disabled={taskAction.isPending} onClick={() => taskAction.mutate({ taskId: activeTaskId, action: activeTaskState === "paused" ? "resume" : "pause" })} className="rounded-lg p-1.5 text-dim hover:bg-hover">{activeTaskState === "paused" ? "Resume" : "Pause"}</button> : null}
          <button type="button" aria-expanded={workspaceOpen} onClick={() => { setSelectedFileId(null); setWorkspaceOpen(!workspaceOpen); setDetailsOpen(false); }} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-ink">Files & workspace{allFiles.length ? ` (${allFiles.length})` : ""}</button>
        </div> : null}
        {AGENTIC_WORKSPACE_ENABLED ? <WorkQueue workspaceId={workspaceId} conversationId={conversationId} tasks={data.tasks} canWrite={canWrite} onChange={invalidate} /> : null}
        {actionError ? (
          <div className="px-4 pt-3 sm:px-8">
            <ErrorNote message={actionError} />
          </div>
        ) : null}

        <Transcript
          items={timeline}
          terminalNotice={toolCalls.error
            ? "Activity history is reconnecting. Saved results remain visible."
            : undefined}
          activityDetail={AGENTIC_WORKSPACE_ENABLED ? activityDetail : "standard"}
          liveRevision={journal.items.filter((item) => item.kind === "generation").map((item) => `${item.id}:${item.revision}`).join(",") + allFiles.map((file) => `${file.id}:${file.version}`).join(",")}
          logsBase={`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/tool-calls`}
          beforeItems={AGENTIC_WORKSPACE_ENABLED ? <>{journal.hasEarlier ? <button type="button" disabled={journal.loadingEarlier} onClick={() => void journal.loadEarlier()} className="mx-auto rounded-lg border border-line px-4 py-2 text-xs">{journal.loadingEarlier ? "Loading…" : "Load earlier activity"}</button> : null}{journal.historyError ? <ErrorNote message={journal.historyError} /> : null}</> : undefined}
          runtimeBase={`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`}
          onOpenFile={(file) => { setSelectedFileId(file.id); setWorkspaceOpen(true); setDetailsOpen(false); }}
          onUseFile={useFile}
          onEditMessage={AGENTIC_WORKSPACE_ENABLED && canWrite ? (message) => { if (message.sender_type === "user") setDraft((current) => ({ ...current, ...messageInputDraft(message.content_json) })); else reuseTurn(message.content_json.text as string || ""); composerRef.current?.focus(); } : undefined}
          onBranchMessage={AGENTIC_WORKSPACE_ENABLED && canWrite && !branch.isPending ? (message) => branch.mutate(message) : undefined}
          agentName={agentName}
          userName={user.display_name}
          pendingApprovals={data.pending_approvals}
          canDecide={canWrite}
          deciding={decide.isPending}
          onApprove={(id) => decide.mutate({ id, decision: "approve" })}
          onReject={(id) => decide.mutate({ id, decision: "reject" })}
          canAnswer={canWrite}
          answering={answerQuestion.isPending}
          onAnswer={onAnswer}
          liveStatus={liveStatus}
          loading={legacyMessages ? messages.isPending : journal.status === "connecting"}
          agentAvatars={avatars}
          agentAvatar={agent ? avatars[agent.id] : null}
          expandExchanges={AGENTIC_WORKSPACE_ENABLED ? activityDetail === "detailed" : detailed}
          resume={data.resume ?? null}
          canRetry={canWrite}
          retrying={resume.isPending}
          onRetry={() => resume.mutate()}
          onReuse={reuseTurn}
        />

        {/* The chats layout already budgets the safe-area inset into its
            height, so plain padding here is enough. */}
        <div className="border-t border-line bg-bg px-4 pb-3 pt-3 sm:px-8">
          <div className="mx-auto max-w-3xl space-y-2">
            <ErrorNote message={sendError} />
            {sendTurn.isPending ? <p role="status" className="text-xs text-dim">Sending message…</p> : null}
            {pendingSend && !sendTurn.isPending && !pendingAcknowledged ? <div className="flex items-center gap-2 text-xs text-warn"><span className="min-w-0 flex-1">Previous message delivery is unconfirmed. Retry uses the same request ID.</span><button type="button" disabled={!canWrite || archived} className="shrink-0 rounded-lg border border-line px-3 py-2" onClick={() => sendTurn.mutate(pendingSend)}>Retry delivery</button></div> : null}
            <Composer
              ref={composerRef}
              value={text}
              onChange={setText}
              onSend={submitTurn}
              sending={sendTurn.isPending}
              disabled={disabledReason !== null}
              uploading={AGENTIC_WORKSPACE_ENABLED && attachments.busy}
              disabledReason={disabledReason}
              placeholder={`Message ${agentName}…`}
              hint={composerHint}
              canStop={canStopTask}
              onStop={() => setConfirmStop(true)}
              stopping={taskAction.isPending}
              stopLabel={`Stop ${agentName}`}
              onFiles={AGENTIC_WORKSPACE_ENABLED ? (selected) => void attachments.addFiles(selected) : undefined}
              hasAttachments={AGENTIC_WORKSPACE_ENABLED && draft.attachment_ids.length > 0}
              attachments={AGENTIC_WORKSPACE_ENABLED ? <><AttachmentTray revisionIds={Object.fromEntries(draft.context_refs.filter((ref) => ref.kind === "file").map((ref) => [ref.id, ref.version_id]))} files={allFiles.filter((file) => draft.attachment_ids.includes(file.id))} uploads={attachments.uploads} onRemove={(id) => setDraft((current) => ({ ...current, attachment_ids: current.attachment_ids.filter((value) => value !== id), context_refs: current.context_refs.filter((reference) => reference.id !== id) }))} onCancel={attachments.cancel} />{draft.context_refs.filter((reference) => reference.kind !== "file").length ? <div className="flex flex-wrap gap-1 px-3 pt-2">{draft.context_refs.filter((reference) => reference.kind !== "file").map((reference) => <button type="button" key={`${reference.kind}:${reference.id}`} className="rounded-lg border border-line px-2 py-1 text-xs text-dim" aria-label={`Remove ${reference.label}`} onClick={() => setDraft((current) => ({ ...current, context_refs: current.context_refs.filter((item) => item !== reference) }))}>@{reference.label} ×</button>)}</div> : null}</> : undefined}
              controls={
                AGENTIC_WORKSPACE_ENABLED ? <TurnControls workspaceId={workspaceId} draft={draft} onChange={(change) => setDraft((current) => ({ ...current, ...change }))} live={live} disabled={disabledReason !== null} onFiles={(selected) => void attachments.addFiles(selected)} files={allFiles} onUseFile={useFile} /> : <ChatComposerControls workspaceId={workspaceId} detail={data} isAdmin={can("admin")} />
              }
            />
            <div className="flex flex-wrap items-center justify-between gap-2"><SecureInputButton disabled={disabledReason !== null || sendTurn.isPending || attachments.busy} onSend={(input) => submitTurn(text, [input])} />{mayContainSecret(text) ? <span className="text-xs text-dim">This credential stays in this tab until sent; its draft is not saved.</span> : null}</div>
          </div>
        </div>
      </section>

      {AGENTIC_WORKSPACE_ENABLED && workspaceOpen ? <Suspense fallback={<div className="p-6"><Spinner label="Opening workspace…" /></div>}><WorkspacePane key={selectedFileId ?? "workspace"} initialFileId={selectedFileId} projectId={conversation.project_id} workspaceId={workspaceId} conversationId={conversationId} files={allFiles} hasMoreFiles={files.hasNextPage} loadingMoreFiles={files.isFetchingNextPage} onLoadMoreFiles={() => void files.fetchNextPage()} isAdmin={can("admin")} canWrite={canWrite} userId={user.id} onUse={useFile} onFeedback={appendFeedback} onClose={() => setWorkspaceOpen(false)} /></Suspense> : null}

      {detailsOpen && wide ? (
        <aside
          aria-label="Chat details"
          className="hidden w-[340px] shrink-0 overflow-y-auto border-l border-line bg-surface px-4 py-5 xl:block"
        >
          {panel}
        </aside>
      ) : null}
      {detailsOpen && !wide ? (
        <Dialog title="Details" open onClose={() => setDetailsOpen(false)}>
          {panel}
        </Dialog>
      ) : null}

      <ConfirmDialog
        open={confirmStop}
        title={`Stop ${agentName}?`}
        body="Stop the active response and its running command. An external action already sent may finish; its outcome will remain visible."
        confirmLabel={`Stop ${agentName}`}
        cancelLabel="Keep going"
        tone="danger"
        busy={taskAction.isPending}
        onConfirm={() => {
          if (activeTaskId) taskAction.mutate({ taskId: activeTaskId, action: "cancel" });
          else setConfirmStop(false);
        }}
        onClose={() => setConfirmStop(false)}
      />
    </div>
  );
}

export default function ChatThreadPage() {
  const conversationId = useSegmentAfter("chats");
  return <ChatThread key={conversationId} conversationId={conversationId} />;
}
