"use client";

/** One chat thread: header, transcript, composer, and the Details panel. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { ChatHeader } from "@/components/chat/chat-header";
import { Composer, type ComposerHandle } from "@/components/chat/composer";
import { ContextPanel } from "@/components/chat/context-panel";
import { Transcript } from "@/components/chat/transcript";
import { Button, Dialog, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  CHAT_DETAILED_STORAGE_KEY,
  groupExchanges,
  mergeTimeline,
  newTurn,
  statusLabelFor,
  withDaySeparators,
} from "@/lib/chat";
import {
  useConversation,
  useConversationActivity,
  useConversationMessages,
  useInvalidateApprovals,
  useInvalidateConversations,
  useAgentAvatarMap,
} from "@/lib/hooks";
import type {
  ConversationMessage,
  ConversationUpdate,
  TurnOut,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

/** Keep polling briefly after a send so the new task shows up. */
const RECENT_SEND_WINDOW_MS = 20_000;

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

export default function ChatThreadPage() {
  const params = useParams<{ id: string }>();
  const conversationId = params.id;
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const avatars = useAgentAvatarMap(workspaceId);
  const queryClient = useQueryClient();
  const invalidate = useInvalidateConversations(workspaceId);
  const invalidateApprovals = useInvalidateApprovals(workspaceId);
  const wide = useMediaQuery("(min-width: 1280px)");

  const [text, setText] = useState("");
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
  const [actionError, setActionError] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<ConversationMessage[]>([]);
  const [recentlySent, setRecentlySent] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const composerRef = useRef<ComposerHandle>(null);

  const detail = useConversation(workspaceId, conversationId, true);
  const conversation = detail.data?.conversation ?? null;
  const liveStatus = conversation ? statusLabelFor(conversation) : null;
  const live = liveStatus !== null || recentlySent;

  // Keep polling for a little while after a send so the new task shows up.
  useEffect(() => {
    if (!recentlySent) return;
    const timer = window.setTimeout(() => setRecentlySent(false), RECENT_SEND_WINDOW_MS);
    return () => window.clearTimeout(timer);
  }, [recentlySent]);
  const messages = useConversationMessages(workspaceId, conversationId, live);
  const activity = useConversationActivity(workspaceId, conversationId, live);

  // Hide optimistic bubbles once the server echoes them back. Agent↔agent
  // exchanges collapse into quiet rows; date markers appear on day changes.
  const primaryAgentId = conversation?.primary_agent_id ?? null;
  const primaryAgentName = conversation?.agent_name ?? null;
  const timeline = useMemo(() => {
    const server = messages.data ?? [];
    const known = new Set(
      server.map((message) =>
        typeof message.content_json.client_turn_id === "string"
          ? message.content_json.client_turn_id
          : message.id,
      ),
    );
    const pending = optimistic.filter(
      (message) => !known.has(String(message.content_json.client_turn_id)),
    );
    const merged = mergeTimeline([...server, ...pending], activity.data?.items ?? [], { detailed });
    return withDaySeparators(groupExchanges(merged, { primaryAgentId, primaryAgentName }));
  }, [messages.data, optimistic, activity.data, detailed, primaryAgentId, primaryAgentName]);

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

  const sendTurn = useMutation({
    mutationFn: (body: { text: string; client_turn_id: string }) =>
      api<TurnOut>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/turns`, {
        method: "POST",
        body,
      }),
    onMutate: (body) => {
      setSendError(null);
      setRecentlySent(true);
      setOptimistic((current) => [
        ...current,
        {
          id: `optimistic-${body.client_turn_id}`,
          task_id: null,
          run_id: null,
          sender_type: "user",
          sender_id: user.id,
          message_type: "text",
          content_json: { text: body.text, client_turn_id: body.client_turn_id },
          created_at: new Date().toISOString(),
          conversation_id: conversationId,
          sender_name: user.display_name,
          agent_id: null,
        },
      ]);
      setText("");
      return { text: body.text, clientTurnId: body.client_turn_id };
    },
    onSuccess: (result, _body, context) => {
      queryClient.setQueryData<ConversationMessage[]>(
        ["conversation-messages", workspaceId, conversationId],
        (current) => {
          if (!current) return [result.message];
          return current.some((message) => message.id === result.message.id)
            ? current
            : [...current, result.message];
        },
      );
      setOptimistic((current) =>
        current.filter((message) => message.content_json.client_turn_id !== context?.clientTurnId),
      );
      invalidate();
    },
    onError: (error, _body, context) => {
      setOptimistic((current) =>
        current.filter((message) => message.content_json.client_turn_id !== context?.clientTurnId),
      );
      setText((current) => current || context?.text || "");
      setSendError(describeError(error, "Couldn't send your message. Check your connection and try again."));
      composerRef.current?.focus();
    },
  });

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

  const taskAction = useMutation({
    mutationFn: ({ taskId, action }: { taskId: string; action: "pause" | "resume" | "cancel" }) =>
      api(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/${action}`, { method: "POST" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) =>
      setActionError(describeError(error, "Couldn't change the work status. Try again.")),
  });

  if (detail.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Spinner label="Opening chat…" />
      </div>
    );
  }

  if (detail.isError || !detail.data || !conversation) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-sm text-ink">
          {notFound
            ? "This chat doesn't exist or was removed."
            : `Couldn't open this chat${detail.error instanceof ApiError ? `: ${detail.error.detail}` : "."}`}
        </p>
        <Link href="/chats">
          <Button>Back to chats</Button>
        </Link>
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

  const composerHint = liveStatus
    ? liveStatus.kind === "working" || liveStatus.kind === "queued"
      ? `${agentName} is busy with your request — anything you send now is added as an instruction.`
      : null
    : null;

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
      <section className="flex min-w-0 flex-1 flex-col" aria-label={conversation.title}>
        <ChatHeader
          conversation={conversation}
          agent={agent}
          avatar={agent ? avatars[agent.id] : null}
          canEdit={canWrite}
          detailsOpen={detailsOpen}
          onToggleDetails={() => setDetailsOpen((open) => !open)}
          detailed={detailed}
          onToggleDetailed={toggleDetailed}
          onRename={(title) => update.mutate({ title })}
          onTogglePin={() => update.mutate({ pinned: !conversation.pinned })}
          onToggleArchive={() => update.mutate({ status: archived ? "active" : "archived" })}
          busy={update.isPending}
        />
        {actionError ? (
          <div className="px-4 pt-3 sm:px-8">
            <ErrorNote message={actionError} />
          </div>
        ) : null}

        <Transcript
          items={timeline}
          agentName={agentName}
          userName={user.display_name}
          pendingApprovals={data.pending_approvals}
          canDecide={canWrite}
          deciding={decide.isPending}
          onApprove={(id) => decide.mutate({ id, decision: "approve" })}
          onReject={(id) => decide.mutate({ id, decision: "reject" })}
          liveStatus={liveStatus}
          loading={messages.isPending}
          agentAvatars={avatars}
          agentAvatar={agent ? avatars[agent.id] : null}
          expandExchanges={detailed}
        />

        <div className="border-t border-line bg-bg px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3 sm:px-8">
          <div className="mx-auto max-w-3xl space-y-2">
            <ErrorNote message={sendError} />
            <Composer
              ref={composerRef}
              value={text}
              onChange={setText}
              onSend={(value) => sendTurn.mutate(newTurn(value))}
              sending={sendTurn.isPending}
              disabled={disabledReason !== null}
              disabledReason={disabledReason}
              placeholder={`Message ${agentName}…`}
              hint={composerHint}
            />
          </div>
        </div>
      </section>

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

      <Dialog title="Stop this work?" open={confirmStop} onClose={() => setConfirmStop(false)}>
        <p className="text-sm text-dim">
          {agentName} will stop what it&apos;s doing on this request. You can always send a new
          message to start again.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setConfirmStop(false)}>Keep going</Button>
          <Button
            variant="danger"
            onClick={() => {
              setConfirmStop(false);
              if (activeTaskId) taskAction.mutate({ taskId: activeTaskId, action: "cancel" });
            }}
          >
            Stop work
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
