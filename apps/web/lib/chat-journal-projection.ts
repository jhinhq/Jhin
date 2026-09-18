import { groupExchanges, mergeTimeline, withDaySeparators, type TranscriptItem } from "./chat";
import type { ConversationItem } from "./agentic-chat";
import type { ManagedFile } from "./workspace-files";
import type { ActivityCard, ConversationMessage, ConversationToolCall } from "./types";

/** A model step finishes before its visible message is committed. Keep the
 * public draft in the timeline through that handoff; tool-step commentary
 * has no separate text message and remains alongside its actions. */
function publicGenerations(
  journal: ConversationItem[],
  messages: ConversationMessage[],
  primaryAgentId: string | null,
): ConversationItem[] {
  const generations = journal.filter((item) =>
    item.kind === "generation" && item.actor.type === "agent" &&
    (primaryAgentId === null || item.actor.id === primaryAgentId),
  );
  const replaced = new Set<string>();
  const receipts = [
    ...messages.filter((message) => message.sender_type === "agent" && message.message_type === "text")
      .map((message) => ({ runId: message.run_id, agentId: message.sender_id, at: message.created_at })),
    // A tombstone carries identity only. Its earlier draft must not return
    // when the saved reply is hidden, deleted, or reloaded from a snapshot.
    ...journal.filter((item) => item.kind === "message" && item.status === "removed" &&
      item.actor.type === "agent" && item.data.message_type === "text")
      .map((item) => ({ runId: item.run_id, agentId: item.actor.id, at: item.created_at })),
  ];
  for (const receipt of receipts) {
    if (!receipt.runId) continue;
    // A run can produce several replies after steering. Each saved reply
    // replaces only its nearest preceding attempt, rather than the whole run.
    const preceding = generations.filter((item) =>
      item.run_id === receipt.runId && item.actor.id === receipt.agentId &&
      item.created_at <= receipt.at,
    )
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
    if (preceding[0]) replaced.add(preceding[0].id);
  }
  return generations.filter((item) =>
    !replaced.has(item.id) && ["running", "streaming", "completed"].includes(item.status) &&
    typeof item.data.text === "string" && item.data.text.trim(),
  );
}

/** Reconcile durable snapshots with legacy clients without inventing execution. */
export function projectChatTimeline({ messages, tools, journal, optimistic, activity, files, conversationId, primaryAgentId, primaryAgentName, detailed, agentic }: {
  messages: ConversationMessage[]; tools: ConversationToolCall[]; journal: ConversationItem[];
  optimistic: ConversationMessage[]; activity: ActivityCard[]; files: ManagedFile[];
  conversationId: string; primaryAgentId: string | null; primaryAgentName: string | null; detailed: boolean; agentic: boolean;
}): TranscriptItem[] {
  const removed = new Set(journal.filter((item) => item.status === "removed").map((item) => item.id));
  const byMessageId = new Map(messages.filter((message) => !removed.has(`message:${message.id}`)).map((message) => [message.id, message]));
  const byCallId = new Map(tools.filter((call) => !removed.has(`tool_call:${call.id}`)).map((call) => [call.id, call]));
  for (const item of journal) {
    if (item.status === "removed" || typeof item.data.id !== "string") continue;
    if (item.kind === "message") {
      const cached = byMessageId.get(item.data.id);
      byMessageId.set(item.data.id, { ...cached, ...item.data, conversation_id: conversationId, sender_name: item.actor.name ?? cached?.sender_name ?? null, agent_id: cached?.agent_id ?? (item.actor.type === "agent" ? item.actor.id : null) } as ConversationMessage);
    } else if (item.kind === "action" && typeof item.data.tool_name === "string") {
      const cached = byCallId.get(item.data.id);
      if (cached?.completed_at && !item.data.completed_at && cached.completed_at > item.created_at) continue;
      byCallId.set(item.data.id, { ...cached, ...item.data, task_id: item.task_id ?? cached?.task_id ?? "", agent_name: item.actor.name ?? cached?.agent_name ?? primaryAgentName ?? "Agent" } as ConversationToolCall);
    }
  }
  const server = [...byMessageId.values()];
  const known = new Set(server.map((message) => typeof message.content_json.client_turn_id === "string" ? message.content_json.client_turn_id : message.id));
  const pending = optimistic.filter((message) => !known.has(String(message.content_json.client_turn_id)));
  const merged = mergeTimeline([...server, ...pending], activity, { detailed, toolCalls: [...byCallId.values()].filter((call) => agentic || call.tool_name.startsWith("cli.")) });
  const items: Exclude<TranscriptItem, { kind: "day" }>[] = groupExchanges(merged, { primaryAgentId, primaryAgentName });
  if (agentic) {
    for (const item of publicGenerations(journal, server, primaryAgentId)) items.push({ kind: "generation", id: item.id, at: item.created_at, item });
    for (const item of journal) if (item.id.startsWith("runtime_session:") && ["terminal", "preview"].includes(String(item.data.kind)) && item.status !== "removed") items.push({ kind: "runtime", id: item.id, at: item.created_at, item });
    for (const file of files) if (file.kind === "artifact") items.push({ kind: "file", id: `file:${file.id}`, at: file.created_at, file });
  }
  items.sort((a, b) => a.at.localeCompare(b.at) || a.id.localeCompare(b.id));
  return withDaySeparators(items);
}
