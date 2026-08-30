"use client";

/** Workspace settings: rename (admin+), the monthly model budget, your own
 * account password, and — for the owner alone — deleting the workspace. People
 * and roles live on their own page (docs/architecture/rbac.md). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { DangerZone } from "@/components/settings/danger-zone";
import { PasswordCard } from "@/components/settings/password-card";
import { Button, Card, ErrorNote, Field, Input, Spinner, focusRing } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useWorkspaceSpend } from "@/lib/hooks";
import {
  dollarInputToMicros,
  formatMicrosAsDollars,
  microsToDollarInput,
  summarizeBudget,
} from "@/lib/models";
import type { Workspace } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export default function SettingsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const workspaceQuery = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api<Workspace>(`/api/v1/workspaces/${workspaceId}`),
  });

  const [name, setName] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: (newName: string) =>
      api<Workspace>(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { name: newName },
      }),
    onSuccess: () => void workspaceQuery.refetch(),
  });

  const currentName = name ?? workspaceQuery.data?.name ?? workspace.workspace_name;
  const isAdmin = can("admin");

  return (
    <>
      <PageHeader
        title="Settings"
        description="Name your workspace, watch what it spends, and manage your own sign-in."
      />
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
          {rename.error ? (
            <div className="mt-3">
              <ErrorNote
                message={
                  rename.error instanceof ApiError
                    ? rename.error.detail
                    : "Renaming the workspace failed — try again."
                }
              />
            </div>
          ) : null}
          <p className="mt-3 text-xs text-faint">
            Slug: <code className="font-mono">{workspaceQuery.data?.slug ?? workspace.workspace_slug}</code> · The
            default model lives on the <Link href="/models" className="text-accent-strong hover:underline">Models page</Link>.
          </p>
        </Card>

        <BudgetCard workspaceId={workspaceId} isAdmin={isAdmin} />

        <Card as="section">
          <h2 className="mb-1 font-display text-base font-semibold">People</h2>
          <p className="mb-4 text-sm text-dim">
            Members, roles, and invitations live on the People page, along with what each role
            is allowed to do.
          </p>
          <Link
            href="/people"
            className={`inline-flex items-center gap-2 rounded-xl border border-line px-3 py-2 text-sm font-medium hover:bg-hover ${focusRing}`}
          >
            <Users size={15} aria-hidden /> Manage people
            <ArrowRight size={14} aria-hidden />
          </Link>
        </Card>

        <PasswordCard />

        {/* Last on the page, on purpose: nothing to scroll past it into. */}
        <DangerZone />
      </PageBody>
    </>
  );
}

/** Tracked model spend plus the monthly budget editor (stored under
 * `settings_json.budget`; the Models page shows the same numbers). */
function BudgetCard({ workspaceId, isAdmin }: { workspaceId: string; isAdmin: boolean }) {
  const spend = useWorkspaceSpend(workspaceId);
  const queryClient = useQueryClient();
  const [budget, setBudget] = useState<string | null>(null);
  const [threshold, setThreshold] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (payload: { monthly_budget_micros: number | null; warning_threshold: number }) =>
      api<Workspace>(`/api/v1/workspaces/${workspaceId}`, {
        method: "PATCH",
        body: { settings: { budget: payload } },
      }),
    onSuccess: () => {
      setBudget(null);
      setThreshold(null);
      void queryClient.invalidateQueries({ queryKey: ["workspace-spend", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
    },
  });

  const data = spend.data;
  const budgetValue = budget ?? microsToDollarInput(data?.monthly_budget_micros ?? null);
  const thresholdValue =
    threshold ?? String(Math.round((data?.warning_threshold ?? 0.8) * 100));
  const summary = data
    ? summarizeBudget(data.spent_month_micros, data.monthly_budget_micros, data.warning_threshold)
    : null;

  return (
    <Card as="section" data-testid="budget-card">
      <h2 className="mb-1 font-display text-base font-semibold">Model spend and budget</h2>
      <p className="mb-4 text-sm text-dim">
        Spend is tracked by Jhin from each run&apos;s token usage and the profile&apos;s prices. Set a
        monthly budget to get a warning bar on the Models page.
      </p>
      {spend.isPending ? (
        <Spinner />
      ) : data ? (
        <dl className="mb-4 grid gap-3 sm:grid-cols-3">
          <div>
            <dt className="text-xs uppercase tracking-wider text-faint">This month</dt>
            <dd className="font-display text-xl font-semibold tabular-nums">
              {formatMicrosAsDollars(data.spent_month_micros)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wider text-faint">All time</dt>
            <dd className="font-display text-xl font-semibold tabular-nums">
              {formatMicrosAsDollars(data.spent_total_micros)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wider text-faint">Budget</dt>
            <dd className={`text-sm ${summary && summary.tone !== "ok" ? "text-danger" : "text-dim"}`}>
              {summary ? summary.label : "Not set"}
            </dd>
          </div>
        </dl>
      ) : (
        <ErrorNote message="Spend could not be loaded." />
      )}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          const percent = Number(thresholdValue);
          save.mutate({
            monthly_budget_micros: dollarInputToMicros(budgetValue),
            warning_threshold: Number.isFinite(percent)
              ? Math.min(Math.max(percent, 0), 100) / 100
              : 0.8,
          });
        }}
      >
        <div className="w-44">
          <Field label="Monthly budget ($)">
            <Input
              type="number"
              min="0"
              step="any"
              value={budgetValue}
              disabled={!isAdmin}
              onChange={(event) => setBudget(event.target.value)}
              placeholder="100"
            />
          </Field>
        </div>
        <div className="w-36">
          <Field label="Warn at (%)">
            <Input
              type="number"
              min="0"
              max="100"
              step="1"
              value={thresholdValue}
              disabled={!isAdmin}
              onChange={(event) => setThreshold(event.target.value)}
            />
          </Field>
        </div>
        {isAdmin ? (
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save budget"}
          </Button>
        ) : null}
        {/* One hint for the row: a hint under a single Field would make the
         * inputs sit at different heights under `items-end`. */}
        <p className="w-full text-[13px] text-faint">
          Leave the budget empty for no limit. The warning shows on Home and Models once
          spending passes that share of the budget.
        </p>
      </form>
      <ErrorNote
        message={save.error ? (save.error instanceof ApiError ? save.error.detail : "Saving failed") : null}
      />
    </Card>
  );
}
