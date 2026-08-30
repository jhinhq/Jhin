"use client";

/** Advanced → Review policies: when an agent's work gets a second look, and
 * by whom. Admin-only editing; others see the rules read-only. */

import { useMutation } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Disclosure, LoadError, SectionCard } from "@/components/company/bits";
import { ReviewPolicyDialog } from "@/components/company/review-policy-dialog";
import { Badge, Button, ConfirmDialog, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { describeCondition, describeReviewer, describeScope, REVIEW_MODE_LABELS } from "@/lib/coordination";
import { useAgents, useInvalidateCoordination, useReviewPolicies, useTeams } from "@/lib/hooks";
import type { ReviewPolicy } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export default function ReviewPoliciesPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const policies = useReviewPolicies(workspaceId);
  const agents = useAgents(workspaceId);
  const teams = useTeams(workspaceId);
  const invalidate = useInvalidateCoordination(workspaceId);
  const [editing, setEditing] = useState<ReviewPolicy | "new" | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ReviewPolicy | null>(null);
  const [error, setError] = useState<string | null>(null);

  const agentName = useMemo(() => {
    const map = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));
    return (id: string) => map.get(id);
  }, [agents.data]);
  const teamName = useMemo(() => {
    const map = new Map((teams.data ?? []).map((team) => [team.id, team.name]));
    return (id: string) => map.get(id);
  }, [teams.data]);

  const fail = (err: unknown, what: string) =>
    setError(`${err instanceof ApiError ? err.detail : what} Nothing changed — try again.`);

  const toggle = useMutation({
    mutationFn: (policy: ReviewPolicy) =>
      api<ReviewPolicy>(`/api/v1/workspaces/${workspaceId}/review-policies/${policy.id}`, {
        method: "PATCH",
        body: { enabled: !policy.enabled },
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => fail(err, "Switching the policy failed."),
  });

  const remove = useMutation({
    mutationFn: (policy: ReviewPolicy) =>
      api(`/api/v1/workspaces/${workspaceId}/review-policies/${policy.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setError(null);
      setConfirmDelete(null);
      invalidate();
    },
    onError: (err) => fail(err, "Deleting the policy failed."),
  });

  return (
    <>
      <PageHeader
        eyebrow="Advanced"
        title="Review policies"
        description="Decide when an agent's work gets a second look before it goes further, and who gives it."
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus size={15} /> New policy
            </Button>
          ) : null
        }
      />
      <PageBody>
        <div className="space-y-4">
          <ErrorNote message={error} />
          {!isAdmin ? (
            <p className="rounded-xl border border-line bg-raised px-4 py-3 text-sm text-dim">
              Only admins can change review policies. You can read the current rules below.
            </p>
          ) : null}
          {policies.isPending ? (
            <Spinner label="Loading policies…" />
          ) : policies.isError || !policies.data ? (
            <LoadError what="review policies" onRetry={() => void policies.refetch()} />
          ) : policies.data.length === 0 ? (
            <EmptyState
              title="No review policies yet"
              description="Without a policy, agents finish work on their own. Add one to have a manager, a colleague, or a person check risky or important work."
              action={
                isAdmin ? (
                  <Button variant="primary" onClick={() => setEditing("new")}>
                    <Plus size={15} /> Create the first policy
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <ul className="space-y-3">
              {policies.data.map((policy) => (
                <SectionCard
                  key={policy.id}
                  title={policy.name}
                  description={`${describeScope(policy, { agent: agentName, team: teamName })} · ${REVIEW_MODE_LABELS[policy.mode] ?? policy.mode}`}
                  action={
                    <div className="flex items-center gap-2">
                      <Badge tone={policy.enabled ? "ok" : "neutral"}>{policy.enabled ? "On" : "Off"}</Badge>
                      {isAdmin ? (
                        <>
                          <Button size="sm" onClick={() => setEditing(policy)}>
                            Edit
                          </Button>
                          <Button size="sm" variant="ghost" disabled={toggle.isPending} onClick={() => toggle.mutate(policy)}>
                            {policy.enabled ? "Turn off" : "Turn on"}
                          </Button>
                          <Button size="sm" variant="ghost" className="text-danger" onClick={() => setConfirmDelete(policy)}>
                            Delete
                          </Button>
                        </>
                      ) : null}
                    </div>
                  }
                >
                  <dl className="grid gap-3 text-sm sm:grid-cols-2">
                    <div>
                      <dt className="text-dim">Triggers when</dt>
                      <dd>
                        {policy.conditions_json.length === 0 ? (
                          <span className="text-faint">never (no conditions)</span>
                        ) : (
                          <ul className="list-disc pl-4">
                            {policy.conditions_json.map((condition, index) => (
                              <li key={`${condition.kind}-${index}`}>{describeCondition(condition)}</li>
                            ))}
                          </ul>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-dim">Reviewed by</dt>
                      <dd>{describeReviewer(policy.reviewer_selector_json, agentName)}</dd>
                      <dt className="mt-2 text-dim">If nobody can review</dt>
                      <dd>{policy.fail_closed ? "Wait for a person (the agent pauses)" : "Skip the review and carry on"}</dd>
                      {policy.mode === "periodic" && policy.period_seconds ? (
                        <>
                          <dt className="mt-2 text-dim">Checks in</dt>
                          <dd>every {Math.round(policy.period_seconds / 60)} minutes</dd>
                        </>
                      ) : null}
                    </div>
                  </dl>
                  <div className="mt-3">
                    <Disclosure label="Show details" openLabel="Hide details">
                      <dl className="grid gap-1 font-mono text-[12px] text-dim sm:grid-cols-2">
                        <div>priority {policy.priority} (lower wins)</div>
                        <div>scope {policy.scope_kind}{policy.scope_id ? ` ${policy.scope_id}` : ""}{policy.scope_key ? ` ${policy.scope_key}` : ""}</div>
                        <div>id {policy.id}</div>
                        <div>updated {policy.updated_at}</div>
                      </dl>
                    </Disclosure>
                  </div>
                </SectionCard>
              ))}
            </ul>
          )}
        </div>
      </PageBody>

      {editing ? (
        <ReviewPolicyDialog
          workspaceId={workspaceId}
          policy={editing === "new" ? null : editing}
          agents={agents.data ?? []}
          teams={teams.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            invalidate();
          }}
        />
      ) : null}

      {confirmDelete ? (
        <ConfirmDialog
          open
          title={`Delete “${confirmDelete.name}”?`}
          body="Reviews already opened by this policy stay as they are. New work simply won’t be checked by this rule anymore."
          confirmLabel="Delete"
          cancelLabel="Keep it"
          busy={remove.isPending}
          onConfirm={() => remove.mutate(confirmDelete)}
          onClose={() => setConfirmDelete(null)}
        />
      ) : null}
    </>
  );
}
