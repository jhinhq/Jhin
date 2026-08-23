"use client";

/** Workspace settings: rename (admin+) and member management (plan 20.2). */

import { useMutation, useQuery } from "@tanstack/react-query";
import { Trash2, UserPlus } from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Badge, Button, Card, ErrorNote, Field, Input, Select, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useMembers } from "@/lib/hooks";
import type { Member, Workspace, WorkspaceRole } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const ASSIGNABLE_ROLES: WorkspaceRole[] = ["viewer", "member", "admin", "owner"];

export default function SettingsPage() {
  const { workspace, user, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const members = useMembers(workspaceId);
  const workspaceQuery = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api<Workspace>(`/api/v1/workspaces/${workspaceId}`),
  });

  const [name, setName] = useState<string | null>(null);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<WorkspaceRole>("member");
  const [memberError, setMemberError] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: (newName: string) =>
      api<Workspace>(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { name: newName },
      }),
    onSuccess: () => void workspaceQuery.refetch(),
  });

  const addMember = useMutation({
    mutationFn: () =>
      api<Member>(`/api/v1/workspaces/${workspaceId}/members`, {
        method: "POST",
        body: { email: inviteEmail, role: inviteRole },
      }),
    onSuccess: () => {
      setInviteEmail("");
      setMemberError(null);
      void members.refetch();
    },
    onError: (error) =>
      setMemberError(error instanceof ApiError ? error.detail : "Adding the member failed"),
  });

  const changeRole = useMutation({
    mutationFn: ({ membershipId, role }: { membershipId: string; role: WorkspaceRole }) =>
      api<Member>(`/api/v1/workspaces/${workspaceId}/members/${membershipId}`, {
        method: "PATCH",
        body: { role },
      }),
    onSuccess: () => {
      setMemberError(null);
      void members.refetch();
    },
    onError: (error) =>
      setMemberError(error instanceof ApiError ? error.detail : "Role change failed"),
  });

  const removeMember = useMutation({
    mutationFn: (membershipId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/members/${membershipId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      setMemberError(null);
      void members.refetch();
    },
    onError: (error) =>
      setMemberError(error instanceof ApiError ? error.detail : "Removal failed"),
  });

  const currentName = name ?? workspaceQuery.data?.name ?? workspace.workspace_name;
  const isAdmin = can("admin");

  return (
    <>
      <PageHeader title="Settings" description="Name your workspace and manage who has access." />
      <PageBody className="max-w-3xl space-y-8">
        <Card as="section">
          <h2 className="mb-4 font-display text-base font-semibold">Workspace</h2>
          <form
            className="flex items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              rename.mutate(currentName);
            }}
          >
            <div className="flex-1">
              <Field label="Name">
                <Input
                  value={currentName}
                  disabled={!isAdmin}
                  maxLength={200}
                  onChange={(event) => setName(event.target.value)}
                />
              </Field>
            </div>
            {isAdmin ? (
              <Button
                type="submit"
                variant="primary"
                disabled={rename.isPending || currentName === workspaceQuery.data?.name}
              >
                {rename.isPending ? "Saving…" : "Rename"}
              </Button>
            ) : null}
          </form>
          <p className="mt-3 text-xs text-faint">
            Slug: <code className="font-mono">{workspaceQuery.data?.slug ?? workspace.workspace_slug}</code> · Default
            model, timezone, and other settings are coming later.
          </p>
        </Card>

        <Card as="section">
          <h2 className="mb-1 font-display text-base font-semibold">Members</h2>
          <p className="mb-4 text-sm text-dim">
            Roles: owner &gt; admin &gt; member &gt; viewer. Only owners may grant or modify the
            owner role.
          </p>
          {isAdmin ? (
            <form
              className="mb-4 flex items-end gap-3"
              onSubmit={(event) => {
                event.preventDefault();
                addMember.mutate();
              }}
            >
              <div className="flex-1">
                <Field label="Add by email" hint="The user must already have an account.">
                  <Input
                    type="email"
                    required
                    value={inviteEmail}
                    onChange={(event) => setInviteEmail(event.target.value)}
                    placeholder="teammate@company.com"
                  />
                </Field>
              </div>
              <div className="w-32">
                <Field label="Role">
                  <Select
                    value={inviteRole}
                    onChange={(event) => setInviteRole(event.target.value as WorkspaceRole)}
                  >
                    {ASSIGNABLE_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
              <Button type="submit" variant="primary" disabled={addMember.isPending}>
                <UserPlus size={14} /> Add
              </Button>
            </form>
          ) : null}
          <ErrorNote message={memberError} />
          {members.isPending ? (
            <Spinner />
          ) : (
            <ul className="mt-2 divide-y divide-line">
              {(members.data ?? []).map((member) => (
                <li key={member.id} className="flex items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {member.display_name}
                      {member.user_id === user.id ? (
                        <span className="text-faint"> (you)</span>
                      ) : null}
                    </p>
                    <p className="truncate text-xs text-dim">{member.email}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {isAdmin ? (
                      <>
                        <Select
                          value={member.role}
                          className="!h-8 w-28 !text-[13px]"
                          onChange={(event) =>
                            changeRole.mutate({
                              membershipId: member.id,
                              role: event.target.value as WorkspaceRole,
                            })
                          }
                        >
                          {ASSIGNABLE_ROLES.map((role) => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </Select>
                        <Button
                          size="sm"
                          variant="ghost"
                          title="Remove member"
                          onClick={() => {
                            if (window.confirm(`Remove ${member.display_name}?`)) {
                              removeMember.mutate(member.id);
                            }
                          }}
                        >
                          <Trash2 size={13} />
                        </Button>
                      </>
                    ) : (
                      <Badge tone={member.role === "owner" ? "accent" : "neutral"}>
                        {member.role}
                      </Badge>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </PageBody>
    </>
  );
}
