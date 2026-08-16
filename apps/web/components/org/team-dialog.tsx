"use client";

/** Create/edit team dialog. Server-side cycle errors (409) surface inline. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input, Select, Textarea } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useInvalidateOrg } from "@/lib/hooks";
import type { OrgAgentNode, OrgTeamNode, Team } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export const TEAM_COLORS = [
  "slate",
  "indigo",
  "violet",
  "cyan",
  "emerald",
  "amber",
  "rose",
] as const;

interface TeamDialogProps {
  open: boolean;
  onClose: () => void;
  /** null = create */
  editing: OrgTeamNode | null;
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
}

export function TeamDialog({ open, ...rest }: TeamDialogProps) {
  // Unmount when closed so the form remounts with fresh state on every open;
  // avoids syncing form state via effects.
  if (!open) return null;
  return <TeamDialogForm key={rest.editing?.id ?? "new"} {...rest} />;
}

function TeamDialogForm({
  onClose,
  editing,
  teams,
  agents,
}: Omit<TeamDialogProps, "open">) {
  const { workspace } = useWorkspace();
  const invalidate = useInvalidateOrg(workspace.workspace_id);
  const [name, setName] = useState(editing?.name ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [parentTeamId, setParentTeamId] = useState(editing?.parent_team_id ?? "");
  const [managerAgentId, setManagerAgentId] = useState(editing?.manager_agent_id ?? "");
  const [colorToken, setColorToken] = useState(editing?.color_token ?? "slate");

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name,
        description,
        parent_team_id: parentTeamId || null,
        manager_agent_id: managerAgentId || null,
        color_token: colorToken,
      };
      return editing
        ? api<Team>(`/api/v1/workspaces/${workspace.workspace_id}/teams/${editing.id}`, {
            method: "PATCH",
            body,
          })
        : api<Team>(`/api/v1/workspaces/${workspace.workspace_id}/teams`, {
            method: "POST",
            body,
          });
    },
    onSuccess: () => {
      invalidate();
      onClose();
    },
  });

  const errorMessage =
    save.error instanceof ApiError ? save.error.detail : save.error ? "Request failed" : null;

  return (
    <Dialog title={editing ? `Edit ${editing.name}` : "New team"} open onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Name">
          <Input
            required
            maxLength={200}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Engineering"
          />
        </Field>
        <Field label="Description">
          <Textarea
            rows={2}
            maxLength={4000}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What this team owns"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Parent team">
            <Select
              value={parentTeamId}
              onChange={(event) => setParentTeamId(event.target.value)}
            >
              <option value="">None (top level)</option>
              {teams
                .filter((team) => team.id !== editing?.id)
                .map((team) => (
                  <option key={team.id} value={team.id}>
                    {team.name}
                  </option>
                ))}
            </Select>
          </Field>
          <Field label="Manager agent">
            <Select
              value={managerAgentId}
              onChange={(event) => setManagerAgentId(event.target.value)}
            >
              <option value="">None</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Color">
          <div className="flex gap-2 pt-1">
            {TEAM_COLORS.map((color) => (
              <button
                key={color}
                type="button"
                title={color}
                aria-label={`color ${color}`}
                aria-pressed={colorToken === color}
                onClick={() => setColorToken(color)}
                className={`team-accent-${color} h-6 w-6 rounded-full border-2 transition-transform ${
                  colorToken === color
                    ? "scale-110 border-ink"
                    : "border-transparent opacity-70 hover:opacity-100"
                }`}
                style={{ background: "var(--team)" }}
              />
            ))}
          </div>
        </Field>
        <ErrorNote message={errorMessage} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : editing ? "Save changes" : "Create team"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
