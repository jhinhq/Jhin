/** Public conversation journal and composer state. No provider reasoning is accepted here. */
import { mayContainSecret } from "@/lib/private-input";
export type ActivityDetail = "compact" | "standard" | "detailed";
export type ExecutionMode = "ask" | "plan" | "act";
export type DeliveryMode = "auto" | "steer" | "queue";
export interface ContextReference { kind: string; id: string; label?: string; version_id?: string; }
export interface ChatDraft { version: 1; text: string; attachment_ids: string[]; context_refs: ContextReference[]; execution_mode: ExecutionMode; model_profile_id?: string; delivery: DeliveryMode; }
export const emptyDraft = (): ChatDraft => ({ version: 1, text: "", attachment_ids: [], context_refs: [], execution_mode: "act", delivery: "auto" });
export function readDraft(workspaceId: string, conversationId: string): ChatDraft {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`jhin-draft-v1:${workspaceId}:${conversationId}`) ?? "null");
    if (value && typeof value === "object" && "version" in value && value.version === 1) {
      const saved = value as Partial<ChatDraft>;
      if (typeof saved.text === "string" && mayContainSecret(saved.text)) { saved.text = ""; localStorage.setItem(`jhin-draft-v1:${workspaceId}:${conversationId}`, JSON.stringify(saved)); }
      return { ...emptyDraft(),
        text: typeof saved.text === "string" && !mayContainSecret(saved.text) ? saved.text : "",
        attachment_ids: Array.isArray(saved.attachment_ids) ? saved.attachment_ids.filter((id) => typeof id === "string") : [],
        context_refs: Array.isArray(saved.context_refs) ? saved.context_refs.filter((ref) => ref && typeof ref.kind === "string" && typeof ref.id === "string") : [],
        execution_mode: ["ask", "plan", "act"].includes(saved.execution_mode ?? "") ? saved.execution_mode! : "act",
        delivery: ["auto", "steer", "queue"].includes(saved.delivery ?? "") ? saved.delivery! : "auto",
        model_profile_id: typeof saved.model_profile_id === "string" ? saved.model_profile_id : undefined,
      };
    }
  } catch { /* Private browsing or an older malformed draft. */ }
  return emptyDraft();
}
export function saveDraft(workspaceId: string, conversationId: string, draft: ChatDraft) {
  try { localStorage.setItem(`jhin-draft-v1:${workspaceId}:${conversationId}`, JSON.stringify({ ...draft, text: mayContainSecret(draft.text) ? "" : draft.text })); } catch { /* In-memory draft still works. */ }
}
/** Edit-and-resubmit keeps the original immutable inputs; it never executes. */
export function messageInputDraft(content: Record<string, unknown>): Pick<ChatDraft, "text" | "attachment_ids" | "context_refs"> {
  const values = [...(Array.isArray(content.attachments) ? content.attachments : []), ...(Array.isArray(content.context_refs) ? content.context_refs : [])];
  const references: ContextReference[] = values.filter((value) => value && typeof value.type === "string" && typeof value.id === "string").map((value) => ({ kind: value.type, id: value.id, ...(typeof value.revision_id === "string" ? { version_id: value.revision_id } : {}), ...(typeof value.name === "string" || typeof value.label === "string" ? { label: value.name ?? value.label } : {}) }));
  return { text: typeof content.text === "string" ? content.text : "", attachment_ids: [...new Set(references.filter((ref) => ref.kind === "file").map((ref) => ref.id))], context_refs: [...new Map(references.map((ref) => [`${ref.kind}:${ref.id}`, ref])).values()] };
}
export interface ConversationItem {
  id: string; version: 1; sequence: number; revision: number;
  kind: "message" | "action" | "task" | "approval" | "question" | "generation" | "delegation" | "file";
  status: string; actor: { type: string; id: string | null; name?: string | null }; task_id: string | null; run_id: string | null; created_at: string; data: Record<string, unknown>;
}
export interface ConversationItemsPage { items: ConversationItem[]; cursor: number; next_before: number | null; has_more: boolean; version: 1; }
export function mergeConversationItems(current: readonly ConversationItem[], incoming: readonly ConversationItem[]): ConversationItem[] {
  const byId = new Map(current.map((item) => [item.id, item]));
  for (const item of incoming) {
    const old = byId.get(item.id);
    if (!old || item.revision > old.revision || (item.revision === old.revision && item.sequence > old.sequence)) byId.set(item.id, item);
  }
  return [...byId.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
}

export interface ServerEvent { event: string; id: string; data: string; }
/** Retain a partial frame between transport chunks; data may contain several lines. */
export function consumeEventFrames(buffer: string): { events: ServerEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = frames.pop() ?? "";
  const events: ServerEvent[] = [];
  for (const frame of frames) {
    const result: ServerEvent = { event: "message", id: "", data: "" };
    const data: string[] = [];
    for (const line of frame.split("\n")) {
      const colon = line.indexOf(":");
      if (colon < 0) continue;
      const field = line.slice(0, colon), value = line.slice(colon + 1).replace(/^ /, "");
      if (field === "data") data.push(value);
      else if (field === "event") result.event = value;
      else if (field === "id" && !value.includes("\0")) result.id = value;
    }
    if (data.length) events.push({ ...result, data: data.join("\n") });
  }
  return { events, rest };
}
