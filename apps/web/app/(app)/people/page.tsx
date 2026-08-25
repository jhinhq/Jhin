"use client";

/** People: who is in the workspace, what they can do, and who has been
 * invited (docs/architecture/rbac.md). */

import { useMutation } from "@tanstack/react-query";
import { Mail, Trash2, UserPlus } from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { OneTimeSecret, ROLE_COPY, ROLE_ORDER, RoleBadge, RoleSelect } from "@/components/access";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useInvitations, useMembers } from "@/lib/hooks";
import { formatRelative } from "@/lib/format";
import type { Invitation, InvitationCreated, Member, WorkspaceRole } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

export default function PeoplePage() {
  const { workspace, user, role, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const isOwner = role === "owner";

  const members = useMembers(workspaceId);
  const invitations = useInvitations(workspaceId, isAdmin);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const changeRole = useMutation({
    mutationFn: ({ membershipId, role: next }: { membershipId: string; role: WorkspaceRole }) =>
      api<Member>(`/api/v1/workspaces/${workspaceId}/members/${membershipId}`, {
        method: "PATCH",
        body: { role: next },
      }),
    onSuccess: () => {
      setError(null);
      void members.refetch();
    },
    onError: (cause) => setError(errorText(cause, "That role change was not allowed.")),
  });

  const removeMember = useMutation({
    mutationFn: (membershipId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/members/${membershipId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      setError(null);
      void members.refetch();
    },
    onError: (cause) => setError(errorText(cause, "Removing that person was not allowed.")),
  });

  // Only an owner may hand out ownership, and only an owner may change or
  // remove an existing admin or owner. The UI mirrors the API so people are
  // not offered actions that will come back as a 403.
  const assignableCeiling: WorkspaceRole = isOwner ? "owner" : "admin";
  const canModify = (member: Member) =>
    member.user_id === user.id || isOwner || (member.role !== "admin" && member.role !== "owner");

  return (
    <>
      <PageHeader
        title="People"
        description="Everyone who can use this workspace, and what each of them is allowed to do."
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={() => setInviteOpen(true)}>
              <UserPlus size={14} /> Invite someone
            </Button>
          ) : null
        }
      />
      <PageBody className="max-w-4xl space-y-8">
        <Card as="section">
          <h2 className="mb-1 font-display text-base font-semibold">What the roles mean</h2>
          <dl className="mt-3 grid gap-3 sm:grid-cols-2">
            {ROLE_ORDER.map((entry) => (
              <div key={entry} className="rounded-xl border border-line px-3 py-2.5">
                <dt className="text-sm font-medium">{ROLE_COPY[entry].label}</dt>
                <dd className="mt-0.5 text-xs text-dim">{ROLE_COPY[entry].blurb}</dd>
              </div>
            ))}
          </dl>
        </Card>

        <Card as="section">
          <h2 className="mb-4 font-display text-base font-semibold">Members</h2>
          <ErrorNote message={error} />
          {members.isPending ? (
            <Spinner />
          ) : (
            <ul className="divide-y divide-line" data-testid="member-list">
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
                    {isAdmin && canModify(member) ? (
                      <>
                        <Select
                          value={member.role}
                          aria-label={`Role for ${member.display_name}`}
                          className="!h-8 w-28 !text-[13px]"
                          onChange={(event) =>
                            changeRole.mutate({
                              membershipId: member.id,
                              role: event.target.value as WorkspaceRole,
                            })
                          }
                        >
                          {ROLE_ORDER.filter(
                            (entry) =>
                              ROLE_ORDER.indexOf(entry) <= ROLE_ORDER.indexOf(assignableCeiling) ||
                              entry === member.role,
                          ).map((entry) => (
                            <option key={entry} value={entry}>
                              {ROLE_COPY[entry].label}
                            </option>
                          ))}
                        </Select>
                        <Button
                          size="sm"
                          variant="ghost"
                          title={
                            member.user_id === user.id
                              ? "Leave this workspace"
                              : `Remove ${member.display_name}`
                          }
                          aria-label={
                            member.user_id === user.id
                              ? "Leave this workspace"
                              : `Remove ${member.display_name}`
                          }
                          onClick={() => {
                            const isSelf = member.user_id === user.id;
                            const question = isSelf
                              ? `Leave ${workspace.workspace_name}? You lose access immediately and will need a new invitation to come back.`
                              : `Remove ${member.display_name} from ${workspace.workspace_name}? They lose access immediately.`;
                            if (window.confirm(question)) removeMember.mutate(member.id);
                          }}
                        >
                          <Trash2 size={13} />
                        </Button>
                      </>
                    ) : (
                      <RoleBadge role={member.role} />
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {isAdmin ? (
          <PendingInvitations
            workspaceId={workspaceId}
            invitations={invitations.data ?? []}
            loading={invitations.isPending}
            onChanged={() => void invitations.refetch()}
          />
        ) : null}
      </PageBody>

      <InviteDialog
        open={inviteOpen}
        workspaceId={workspaceId}
        maxRole={assignableCeiling}
        onClose={() => setInviteOpen(false)}
        onCreated={() => {
          void invitations.refetch();
          void members.refetch();
        }}
      />
    </>
  );
}

function InviteDialog({
  open,
  workspaceId,
  maxRole,
  onClose,
  onCreated,
}: {
  open: boolean;
  workspaceId: string;
  maxRole: WorkspaceRole;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("member");
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invite = useMutation({
    mutationFn: () =>
      api<InvitationCreated>(`/api/v1/workspaces/${workspaceId}/invitations`, {
        method: "POST",
        body: { email, role },
      }),
    onSuccess: (result) => {
      setCreated(result);
      setError(null);
      setEmail("");
      onCreated();
    },
    onError: (cause) => setError(errorText(cause, "That invitation could not be created.")),
  });

  const close = () => {
    setCreated(null);
    setError(null);
    onClose();
  };

  return (
    <Dialog
      title={created ? "Invitation ready" : "Invite someone"}
      open={open}
      onClose={close}
      description={
        created
          ? undefined
          : "Jhin does not send email. You will get a link to pass on however you like."
      }
    >
      {created ? (
        <div className="space-y-4">
          <OneTimeSecret
            testId="invite-link"
            label={`Invite link for ${created.invitation.email}`}
            value={created.invite_url}
            warning="Copy it now — this link is shown once, works once, and expires. Send it over a channel you trust."
          />
          <p className="text-sm text-dim">
            They will choose their own password when they open it. Nobody, including you, ever
            sees it.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setCreated(null)}>
              Invite someone else
            </Button>
            <Button variant="primary" onClick={close}>
              Done
            </Button>
          </div>
        </div>
      ) : (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            invite.mutate();
          }}
        >
          <Field label="Email address">
            <Input
              type="email"
              required
              value={email}
              placeholder="teammate@company.com"
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <RoleSelect value={role} onChange={setRole} maxRole={maxRole} />
          <ErrorNote message={error} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" type="button" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" type="submit" disabled={invite.isPending}>
              {invite.isPending ? "Creating…" : "Create invite link"}
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function PendingInvitations({
  workspaceId,
  invitations,
  loading,
  onChanged,
}: {
  workspaceId: string;
  invitations: Invitation[];
  loading: boolean;
  onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/invitations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setError(null);
      onChanged();
    },
    onError: (cause) => setError(errorText(cause, "That invitation could not be revoked.")),
  });

  const open = invitations.filter((invite) => invite.status !== "accepted");

  return (
    <Card as="section">
      <h2 className="mb-1 font-display text-base font-semibold">Invitations</h2>
      <p className="mb-4 text-sm text-dim">
        Links you have created but nobody has used yet. Revoke one to make it stop working.
      </p>
      <ErrorNote message={error} />
      {loading ? (
        <Spinner />
      ) : open.length === 0 ? (
        <EmptyState
          icon={<Mail size={20} aria-hidden />}
          title="No invitations waiting"
          description="Invite a colleague and their link will show up here until they use it."
        />
      ) : (
        <ul className="divide-y divide-line" data-testid="invitation-list">
          {open.map((invite) => (
            <li key={invite.id} className="flex items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{invite.email}</p>
                <p className="truncate text-xs text-dim">
                  {ROLE_COPY[invite.role].label} ·{" "}
                  {invite.status === "pending"
                    ? `expires ${formatRelative(invite.expires_at)}`
                    : invite.status}
                  {invite.invited_by_name ? ` · invited by ${invite.invited_by_name}` : ""}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Badge tone={invite.status === "pending" ? "info" : "neutral"}>
                  {invite.status}
                </Badge>
                {invite.status === "pending" ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Revoke the invitation for ${invite.email}`}
                    onClick={() => revoke.mutate(invite.id)}
                  >
                    <Trash2 size={13} />
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
