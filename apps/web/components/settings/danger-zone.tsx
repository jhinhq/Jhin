"use client";

/** Deleting a workspace: the one action in Jhin that destroys other people's
 * work as well as your own.
 *
 * Three things earn their place here. The confirmation names what is actually
 * in the workspace, counted live by the API rather than guessed from whatever
 * happens to be in the query cache — a dialog that says "12 agents" and is
 * wrong is worse than one that says nothing. It says out loud that the data
 * belongs to every member, not just the person clicking. And it makes you type
 * the workspace's name, because a destructive button you can hit by muscle
 * memory is not a confirmation. Owners only; admins do not see this at all.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useWorkspaceDeletionSummary } from "@/lib/hooks";
import type { WorkspaceDeletionSummary } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

type CountKey = Exclude<keyof WorkspaceDeletionSummary, "workspace_id" | "name" | "members">;

/** Singular/plural noun for each counted category, in the order the dialog
 *  lists them. `members` is deliberately absent: removing a membership is not
 *  deleting a person's account, and it is worded separately below. */
const CATEGORIES: { key: CountKey; one: string; many: string }[] = [
  { key: "agents", one: "agent", many: "agents" },
  { key: "teams", one: "team", many: "teams" },
  { key: "conversations", one: "chat", many: "chats" },
  { key: "messages", one: "message", many: "messages" },
  { key: "tasks", one: "task", many: "tasks" },
  { key: "memories", one: "memory", many: "memories" },
  { key: "skills", one: "skill", many: "skills" },
  { key: "connections", one: "connected app", many: "connected apps" },
  { key: "triggers", one: "automation", many: "automations" },
  { key: "api_keys", one: "API key", many: "API keys" },
  { key: "secrets", one: "secret", many: "secrets" },
];

/** The non-empty categories, as "12 agents" phrases. Zero counts are dropped:
 *  listing "0 automations" pads the warning with things that are not at risk
 *  and buries the ones that are. */
export function describeDeletion(summary: WorkspaceDeletionSummary): string[] {
  const parts: string[] = [];
  for (const { key, one, many } of CATEGORIES) {
    const count = summary[key];
    if (count > 0) parts.push(`${count.toLocaleString()} ${count === 1 ? one : many}`);
  }
  return parts;
}

export function DangerZone() {
  const { workspace, can } = useWorkspace();
  const [open, setOpen] = useState(false);

  // Not "disabled for everyone else": an admin must never see the section, the
  // button, or a hint that the capability exists.
  if (!can("owner")) return null;

  return (
    <section
      data-testid="danger-zone"
      // Red border, ordinary surface: the section is set apart without tinting
      // the ground under the one control that is supposed to look alarming.
      className="rounded-2xl border border-danger/40 bg-surface p-5 shadow-card"
    >
      <h2 className="mb-1 flex items-center gap-2 font-display text-base font-semibold text-danger">
        <AlertTriangle size={16} aria-hidden /> Danger zone
      </h2>
      <p className="mb-4 text-sm text-dim">
        Deleting {workspace.workspace_name} erases it for everyone in it — agents, chats, tasks,
        memories, skills, connected apps and keys — permanently. Only you, as the owner, can do
        this.
      </p>
      <Button variant="danger" onClick={() => setOpen(true)}>
        Delete this workspace
      </Button>
      {open ? <DeleteDialog onClose={() => setOpen(false)} /> : null}
    </section>
  );
}

function DeleteDialog({ onClose }: { onClose: () => void }) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const name = workspace.workspace_name;
  const queryClient = useQueryClient();
  const router = useRouter();
  const [typed, setTyped] = useState("");

  const summary = useWorkspaceDeletionSummary(workspaceId, true);

  const remove = useMutation({
    mutationFn: () => api<void>(`/api/v1/workspaces/${workspaceId}`, { method: "DELETE" }),
    onSuccess: () => {
      // Every cached query is now about a workspace that no longer exists, so
      // none of it may be reused — including `/auth/me`, whose membership list
      // is what the shell picks the current workspace from. Clearing it and
      // sending the user to Home makes the shell re-derive everything: their
      // next workspace if they have one, and its own "no workspace membership"
      // screen if this was the last.
      queryClient.clear();
      router.replace("/home");
    },
  });

  const busy = remove.isPending;
  // Stable across renders: `Dialog` re-runs its focus trap whenever `onClose`
  // changes identity, and a fresh closure every render would steal focus back
  // from the field on every keystroke.
  const handleClose = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);

  // Trimmed so a trailing space from a copy-paste is not a puzzle, but
  // otherwise exact, case included. The point of the gate is that you read it.
  const confirmed = typed.trim() === name;
  const counts = summary.data ? describeDeletion(summary.data) : null;

  return (
    <Dialog title="Delete this workspace" open onClose={handleClose}>
      <div className="space-y-4">
        <p className="text-sm text-ink">
          This deletes <strong>{name}</strong> and everything in it. It cannot be undone, and it
          takes the data away from <strong>every member</strong>, not only you.
        </p>

        {summary.isPending ? (
          <Spinner label="Counting what is in this workspace…" />
        ) : summary.data ? (
          <div className="rounded-xl border border-line bg-bg px-3.5 py-3 text-sm">
            {counts && counts.length > 0 ? (
              <>
                <p className="mb-1.5 font-medium">Deleted immediately:</p>
                <ul className="list-disc space-y-0.5 pl-5 text-dim">
                  {counts.map((part) => (
                    <li key={part}>{part}</li>
                  ))}
                </ul>
                <p className="mt-2 text-[13px] text-faint">
                  …along with every run, approval and tool call behind them.
                </p>
              </>
            ) : (
              <p className="text-dim">This workspace is empty — there is nothing in it to lose.</p>
            )}
            <p className="mt-2 text-[13px] text-faint">
              {summary.data.members === 1
                ? "You are its only member. Your Jhin account is not deleted, only this workspace."
                : `${summary.data.members} people lose access to it. Their Jhin accounts are not deleted, only this workspace.`}
            </p>
          </div>
        ) : (
          // Never invent numbers to fill the gap: say the count is missing and
          // let the owner decide with that in mind.
          <ErrorNote
            message={
              "The contents of this workspace could not be counted just now, so this dialog " +
              "cannot tell you what is in it. Deleting still removes everything."
            }
          />
        )}

        <Field
          label="Type the workspace name to confirm"
          hint="Exactly as shown above, so there is no doubt which workspace this is."
        >
          <Input
            value={typed}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            aria-label={`Type ${name} to confirm deletion`}
            placeholder={name}
            onChange={(event) => setTyped(event.target.value)}
          />
        </Field>

        <ErrorNote
          message={
            remove.error
              ? remove.error instanceof ApiError
                ? remove.error.detail
                : "The workspace could not be deleted."
              : null
          }
        />

        <div className="flex justify-end gap-2">
          <Button onClick={handleClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" disabled={!confirmed || busy} onClick={() => remove.mutate()}>
            {busy ? "Deleting…" : "Delete workspace permanently"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
