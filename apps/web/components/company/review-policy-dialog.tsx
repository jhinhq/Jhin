"use client";

/** Create / edit one review policy in plain language. Validation mirrors
 * the API's shape rules (lib/coordination.ts) so mistakes are caught before
 * the request. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Disclosure } from "@/components/company/bits";
import { Button, Dialog, ErrorNote, Field, Input, Select } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  CONDITION_SPECS,
  draftFromPolicy,
  emptyPolicyDraft,
  policyDraftToBody,
  rawToThreshold,
  REVIEW_MODE_HELP,
  REVIEW_MODE_LABELS,
  REVIEWER_KIND_LABELS,
  SCOPE_KIND_LABELS,
  thresholdToRaw,
  validatePolicyDraft,
  type ReviewPolicyDraft,
} from "@/lib/coordination";
import type {
  Agent,
  ReviewCondition,
  ReviewMode,
  ReviewPolicy,
  ReviewScopeKind,
  ReviewerKind,
  Team,
} from "@/lib/types";

const MODES: ReviewMode[] = ["before_close", "pre_action", "post_action", "periodic"];
const SCOPES: ReviewScopeKind[] = ["workspace", "team", "agent", "task_type"];
const REVIEWERS: ReviewerKind[] = ["reporting_manager", "agent", "team_role", "human"];

const UNIT_LABELS = { dollars: "$", tokens: "tokens", minutes: "min", confidence: "%" } as const;

export function ReviewPolicyDialog({
  workspaceId,
  policy,
  agents,
  teams,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  policy: ReviewPolicy | null;
  agents: Pick<Agent, "id" | "name" | "role_title">[];
  teams: Pick<Team, "id" | "name">[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<ReviewPolicyDraft>(() => (policy ? draftFromPolicy(policy) : emptyPolicyDraft()));
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      const body = policyDraftToBody(draft);
      if (policy) {
        // PATCH cannot change scope; send the editable fields only.
        const { name, enabled, mode, conditions, reviewer, fail_closed, priority, period_seconds } = body;
        return api<ReviewPolicy>(`/api/v1/workspaces/${workspaceId}/review-policies/${policy.id}`, {
          method: "PATCH",
          body: { name, enabled, mode, conditions, reviewer, fail_closed, priority, period_seconds: period_seconds ?? null },
        });
      }
      return api<ReviewPolicy>(`/api/v1/workspaces/${workspaceId}/review-policies`, { method: "POST", body });
    },
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (err) => setError(`${err instanceof ApiError ? err.detail : "Saving failed"}. Nothing changed — check the form and try again.`),
  });

  const update = (patch: Partial<ReviewPolicyDraft>) => setDraft((current) => ({ ...current, ...patch }));

  const conditionOn = (kind: ReviewCondition["kind"]) => draft.conditions.some((condition) => condition.kind === kind);
  const toggleCondition = (kind: ReviewCondition["kind"]) => {
    const spec = CONDITION_SPECS.find((item) => item.kind === kind);
    if (conditionOn(kind)) {
      update({ conditions: draft.conditions.filter((condition) => condition.kind !== kind) });
    } else {
      const threshold = spec?.unit ? thresholdToRaw(spec.unit, spec.defaultThreshold ?? 0) : undefined;
      update({ conditions: [...draft.conditions, spec?.unit ? { kind, threshold } : { kind }] });
    }
  };
  const setThreshold = (kind: ReviewCondition["kind"], human: string) => {
    const spec = CONDITION_SPECS.find((item) => item.kind === kind);
    if (!spec?.unit) return;
    const value = Number(human);
    update({
      conditions: draft.conditions.map((condition) =>
        condition.kind === kind ? { kind, threshold: Number.isFinite(value) ? thresholdToRaw(spec.unit!, value) : NaN } : condition,
      ),
    });
  };

  return (
    <Dialog title={policy ? "Edit review policy" : "New review policy"} open onClose={onClose} wide>
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          const problem = validatePolicyDraft(draft);
          if (problem) {
            setError(problem);
            return;
          }
          save.mutate();
        }}
      >
        <Field label="Name">
          <Input value={draft.name} maxLength={200} autoFocus onChange={(event) => update({ name: event.target.value })} placeholder="e.g. Check risky actions by new agents" />
        </Field>

        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-dim">Applies to</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            <Select
              aria-label="Who this applies to"
              value={draft.scope_kind}
              disabled={policy !== null}
              onChange={(event) => update({ scope_kind: event.target.value as ReviewScopeKind, scope_id: "", scope_key: "" })}
            >
              {SCOPES.map((kind) => (
                <option key={kind} value={kind}>
                  {SCOPE_KIND_LABELS[kind]}
                </option>
              ))}
            </Select>
            {draft.scope_kind === "team" ? (
              <Select aria-label="Team" value={draft.scope_id} disabled={policy !== null} onChange={(event) => update({ scope_id: event.target.value })}>
                <option value="">Choose a team…</option>
                {teams.map((team) => (
                  <option key={team.id} value={team.id}>
                    {team.name}
                  </option>
                ))}
              </Select>
            ) : null}
            {draft.scope_kind === "agent" ? (
              <Select aria-label="Agent" value={draft.scope_id} disabled={policy !== null} onChange={(event) => update({ scope_id: event.target.value })}>
                <option value="">Choose an agent…</option>
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </Select>
            ) : null}
            {draft.scope_kind === "task_type" ? (
              <Input aria-label="Kind of task" value={draft.scope_key} disabled={policy !== null} maxLength={100} placeholder="e.g. deploy" onChange={(event) => update({ scope_key: event.target.value })} />
            ) : null}
          </div>
          {policy ? <p className="text-xs text-faint">Who a policy applies to can’t change after it’s created; make a new one instead.</p> : null}
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-dim">When</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {MODES.map((mode) => (
              <label key={mode} className={`flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2.5 ${draft.mode === mode ? "border-accent bg-accent-soft" : "border-line bg-surface hover:border-line-strong"}`}>
                <input type="radio" name="mode" value={mode} checked={draft.mode === mode} onChange={() => update({ mode })} className="mt-1 accent-[var(--accent)]" />
                <span>
                  <span className="block text-sm font-medium text-ink">{REVIEW_MODE_LABELS[mode]}</span>
                  <span className="block text-xs text-dim">{REVIEW_MODE_HELP[mode]}</span>
                </span>
              </label>
            ))}
          </div>
          {draft.mode === "periodic" ? (
            <Field label="Check in every (minutes)">
              <Input type="number" min={1} max={30 * 24 * 60} value={draft.period_minutes} onChange={(event) => update({ period_minutes: event.target.value })} className="max-w-[12rem]" />
            </Field>
          ) : null}
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-dim">In which situations</legend>
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {CONDITION_SPECS.map((spec) => {
              const on = conditionOn(spec.kind);
              const current = draft.conditions.find((condition) => condition.kind === spec.kind);
              const human = current?.threshold !== undefined && current.threshold !== null && spec.unit && Number.isFinite(current.threshold)
                ? spec.unit === "confidence"
                  ? Math.round(rawToThreshold(spec.unit, current.threshold) * 100)
                  : rawToThreshold(spec.unit, current.threshold)
                : "";
              return (
                <li key={spec.kind} className={`rounded-xl border px-3 py-2 ${on ? "border-accent/50 bg-accent-soft/50" : "border-line"}`}>
                  <label className="flex cursor-pointer items-start gap-2.5">
                    <input type="checkbox" checked={on} onChange={() => toggleCondition(spec.kind)} className="mt-1 accent-[var(--accent)]" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-ink">{spec.label}</span>
                      <span className="block text-xs text-dim">{spec.help}</span>
                    </span>
                  </label>
                  {on && spec.unit ? (
                    <label className="mt-2 flex items-center gap-2 pl-6 text-xs text-dim">
                      <span>{spec.unit === "confidence" ? "Below" : "Over"}</span>
                      <Input
                        type="number"
                        min={0}
                        step={spec.unit === "dollars" ? 0.01 : 1}
                        value={human}
                        aria-label={`${spec.label} limit`}
                        onChange={(event) => setThreshold(spec.kind, spec.unit === "confidence" ? String(Number(event.target.value) / 100) : event.target.value)}
                        className="h-8 max-w-[8rem] text-sm"
                      />
                      <span>{UNIT_LABELS[spec.unit]}</span>
                    </label>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-dim">Reviewed by</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            <Select aria-label="Reviewer" value={draft.reviewer.kind} onChange={(event) => update({ reviewer: { ...draft.reviewer, kind: event.target.value as ReviewerKind } })}>
              {REVIEWERS.map((kind) => (
                <option key={kind} value={kind}>
                  {REVIEWER_KIND_LABELS[kind]}
                </option>
              ))}
            </Select>
            {draft.reviewer.kind === "agent" ? (
              <Select aria-label="Reviewing agent" value={draft.reviewer.agent_id ?? ""} onChange={(event) => update({ reviewer: { ...draft.reviewer, agent_id: event.target.value || null } })}>
                <option value="">Choose an agent…</option>
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </Select>
            ) : null}
            {draft.reviewer.kind === "team_role" ? (
              <Input aria-label="Role label" placeholder="e.g. Tech lead" maxLength={100} value={draft.reviewer.role_label ?? ""} onChange={(event) => update({ reviewer: { ...draft.reviewer, role_label: event.target.value } })} />
            ) : null}
          </div>
          {draft.reviewer.kind !== "human" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="If they can't (backup agent, optional)">
                <Select value={draft.reviewer.fallback_agent_id ?? ""} onChange={(event) => update({ reviewer: { ...draft.reviewer, fallback_agent_id: event.target.value || null } })}>
                  <option value="">No backup agent</option>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <label className="flex items-center gap-2.5 self-end pb-2 text-sm">
                <input type="checkbox" checked={draft.reviewer.fallback_to_human ?? true} onChange={(event) => update({ reviewer: { ...draft.reviewer, fallback_to_human: event.target.checked } })} className="accent-[var(--accent)]" />
                Ask a person when no agent can review
              </label>
            </div>
          ) : null}
          <p className="text-xs text-faint">An agent never reviews its own work, and paused agents are skipped.</p>
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-dim">If nobody can review</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className={`flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2.5 ${draft.fail_closed ? "border-accent bg-accent-soft" : "border-line"}`}>
              <input type="radio" name="fail_closed" checked={draft.fail_closed} onChange={() => update({ fail_closed: true })} className="mt-1 accent-[var(--accent)]" />
              <span>
                <span className="block text-sm font-medium text-ink">Wait for a person</span>
                <span className="block text-xs text-dim">Safer. The work pauses and shows up in Attention.</span>
              </span>
            </label>
            <label className={`flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2.5 ${!draft.fail_closed ? "border-accent bg-accent-soft" : "border-line"}`}>
              <input type="radio" name="fail_closed" checked={!draft.fail_closed} onChange={() => update({ fail_closed: false })} className="mt-1 accent-[var(--accent)]" />
              <span>
                <span className="block text-sm font-medium text-ink">Skip the review</span>
                <span className="block text-xs text-dim">The agent carries on; the skip is recorded.</span>
              </span>
            </label>
          </div>
        </fieldset>

        <Disclosure label="Show advanced options" openLabel="Hide advanced options">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Priority" hint="When several policies match, the lowest number wins.">
              <Input type="number" min={0} max={10000} value={draft.priority} onChange={(event) => update({ priority: Number(event.target.value) })} />
            </Field>
            <label className="flex items-center gap-2.5 self-end pb-2 text-sm">
              <input type="checkbox" checked={draft.enabled} onChange={(event) => update({ enabled: event.target.checked })} className="accent-[var(--accent)]" />
              Policy is on
            </label>
          </div>
        </Disclosure>

        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : policy ? "Save changes" : "Create policy"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
