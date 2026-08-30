"use client";

/**
 * In-chat quick controls: the per-chat things people want to change without
 * leaving the conversation — which model the agent runs on, how cautious it
 * is before acting, what it can currently reach, and what this chat has cost.
 *
 * Rendered as a popover anchored to a header button so opening it never
 * shifts the transcript or covers the composer. Everything is readable by
 * anyone who can open the chat; the two mutations are admin-only (the API
 * enforces admin on `PATCH /agents/{id}` and `PUT /agents/{id}/policy`), and
 * non-admins see the current value with a plain reason instead of a control.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, SlidersHorizontal } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { PRESET_FRIENDLY, describeGrant, policySummary } from "@/components/company/agent-helpers";
import { ErrorNote, Select, focusRing } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatCostMicros, formatTokens } from "@/lib/format";
import {
  useAgent,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useInvalidateAgentAccess,
  useModelProfiles,
  useTools,
} from "@/lib/hooks";
import type { ApprovalPreset, ConversationDetail } from "@/lib/types";

const PRESETS: ApprovalPreset[] = ["autonomous", "balanced", "restricted"];

const PRESET_LABELS: Record<ApprovalPreset, string> = {
  autonomous: "Free rein",
  balanced: "Balanced",
  restricted: "Careful",
};

/** Shown in place of a control when the viewer can't change something. */
const ADMIN_ONLY = "Only admins can change this.";

const MAX_TOOL_LINES = 4;

function Row({
  title,
  children,
  note,
}: {
  title: string;
  children: React.ReactNode;
  note?: string | null;
}) {
  return (
    <section className="space-y-1.5">
      <h4 className="text-[11px] font-medium uppercase tracking-wider text-faint">{title}</h4>
      {children}
      {note ? <p className="text-xs text-dim">{note}</p> : null}
    </section>
  );
}

export function ChatQuickControls({
  workspaceId,
  detail,
  isAdmin,
}: {
  workspaceId: string;
  detail: ConversationDetail;
  /** Workspace role is admin or owner — the floor the API enforces for both
   * `PATCH /agents/{id}` and `PUT /agents/{id}/policy`. */
  isAdmin: boolean;
}) {
  const agentId = detail.agent?.id ?? null;
  const agentName = detail.agent?.name ?? detail.conversation.agent_name ?? "this agent";
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on Escape or a click outside; the popover is not modal, so the
  // transcript stays scrollable behind it.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        aria-label="Chat settings"
        title="Chat settings"
        aria-expanded={open}
        aria-haspopup="dialog"
        data-testid="quick-controls-trigger"
        onClick={() => setOpen((current) => !current)}
        className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors ${
          open ? "bg-accent-soft text-accent-strong" : "text-dim hover:bg-hover hover:text-ink"
        } ${focusRing}`}
      >
        <SlidersHorizontal size={17} />
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label={`Settings for ${agentName}`}
          data-testid="quick-controls-panel"
          className="absolute right-0 top-full z-40 mt-1 max-h-[min(32rem,calc(100dvh-12rem))] w-[20rem] max-w-[calc(100vw-1.5rem)] space-y-4 overflow-y-auto overscroll-contain rounded-2xl border border-line bg-surface p-4 text-sm shadow-card"
        >
          <ErrorNote message={error} />
          {agentId ? (
            <QuickControlsBody
              workspaceId={workspaceId}
              agentId={agentId}
              agentName={agentName}
              isAdmin={isAdmin}
              onError={setError}
            />
          ) : (
            <p className="text-dim">
              This agent is no longer in the workspace, so there is nothing to change here.
            </p>
          )}
          <Usage detail={detail} />
        </div>
      ) : null}
    </div>
  );
}

function Usage({ detail }: { detail: ConversationDetail }) {
  const tokens = detail.total_input_tokens + detail.total_output_tokens;
  return (
    <Row title="This chat">
      <p data-testid="quick-controls-usage" className="tabular-nums text-ink">
        {formatTokens(tokens)} tokens · {formatCostMicros(detail.total_cost_micros)}
      </p>
      <p className="text-xs text-faint">
        {formatTokens(detail.total_input_tokens)} in · {formatTokens(detail.total_output_tokens)} out
      </p>
    </Row>
  );
}

function QuickControlsBody({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
  onError,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  isAdmin: boolean;
  onError: (message: string | null) => void;
}) {
  const agent = useAgent(workspaceId, agentId);
  const profiles = useModelProfiles(workspaceId);
  const policy = useAgentPolicy(workspaceId, agentId);
  const grants = useAgentGrants(workspaceId, agentId);
  const tools = useTools(workspaceId);
  // Connection names are only used to sharpen a grant's wording, and the
  // endpoint is admin-only — skip the request for everyone else.
  const connections = useConnections(workspaceId, isAdmin);
  const queryClient = useQueryClient();
  const invalidateAccess = useInvalidateAgentAccess(workspaceId, agentId);

  const connectionNames = useMemo(
    () =>
      Object.fromEntries(
        (connections.data ?? []).map((connection) => [connection.id, connection.name]),
      ),
    [connections.data],
  );

  const invalidateAgent = () => {
    invalidateAccess();
    void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
  };

  const setModel = useMutation({
    mutationFn: (modelProfileId: string | null) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`, {
        method: "PATCH",
        body: { model_profile_id: modelProfileId },
      }),
    onSuccess: () => {
      onError(null);
      invalidateAgent();
    },
    onError: (mutationError) =>
      onError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Couldn't change the model. Try again.",
      ),
  });

  const setPreset = useMutation({
    mutationFn: (preset: ApprovalPreset) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/policy`, {
        method: "PUT",
        body: { preset },
      }),
    onSuccess: () => {
      onError(null);
      invalidateAgent();
    },
    onError: (mutationError) =>
      onError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Couldn't change the mode. Try again.",
      ),
  });

  const currentProfileId = agent.data?.model_profile_id ?? null;
  const currentProfile = (profiles.data ?? []).find(
    (profile) => profile.id === currentProfileId,
  );
  const modelLabel = currentProfile
    ? currentProfile.display_name
    : currentProfileId
      ? "A model that is no longer set up"
      : "Workspace default";

  const currentPreset = policy.data?.preset ?? null;
  const allowed = (grants.data ?? []).filter((grant) => grant.effect === "allow");

  return (
    <>
      <Row
        title="Model"
        note={isAdmin ? null : `${ADMIN_ONLY} ${agentName} runs on ${modelLabel}.`}
      >
        {isAdmin ? (
          <>
            <Select
              aria-label="Model"
              value={currentProfileId ?? ""}
              disabled={profiles.isPending || setModel.isPending}
              onChange={(event) => setModel.mutate(event.target.value || null)}
            >
              <option value="">Workspace default</option>
              {(profiles.data ?? []).map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.display_name}
                </option>
              ))}
            </Select>
            <p className="text-xs text-dim">Applies to {agentName} everywhere, from the next turn.</p>
          </>
        ) : (
          <p data-testid="quick-model-readonly" className="text-ink">
            {modelLabel}
          </p>
        )}
      </Row>

      <Row title="Mode" note={isAdmin ? null : `${ADMIN_ONLY} ${policySummary(policy.data)}.`}>
        {isAdmin ? (
          <>
            <div className="flex gap-1" role="group" aria-label="Mode">
              {PRESETS.map((preset) => {
                const active = currentPreset === preset;
                return (
                  <button
                    key={preset}
                    type="button"
                    data-testid={`quick-mode-${preset}`}
                    aria-pressed={active}
                    disabled={setPreset.isPending}
                    onClick={() => setPreset.mutate(preset)}
                    className={`min-h-10 flex-1 rounded-lg border px-2 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 md:min-h-0 ${
                      active
                        ? "border-accent bg-accent-soft text-accent-strong"
                        : "border-line text-dim hover:border-line-strong hover:text-ink"
                    } ${focusRing}`}
                  >
                    {PRESET_LABELS[preset]}
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-dim">
              {currentPreset
                ? PRESET_FRIENDLY[currentPreset]
                : policySummary(policy.data)}
            </p>
          </>
        ) : (
          <p data-testid="quick-mode-readonly" className="text-ink">
            {policySummary(policy.data)}
          </p>
        )}
      </Row>

      <Row title="Tools">
        {grants.isPending || tools.isPending ? (
          <p className="text-dim">Loading…</p>
        ) : allowed.length === 0 ? (
          <p className="text-dim">
            No apps or tools yet{isAdmin ? "" : " — ask an admin to give it some"}.
          </p>
        ) : (
          <ul className="space-y-1 text-[13px] text-ink">
            {allowed.slice(0, MAX_TOOL_LINES).map((grant) => (
              <li key={grant.id} className="flex items-start gap-2">
                <span aria-hidden className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-ok" />
                <span className="min-w-0">
                  {describeGrant(grant, tools.data ?? [], connectionNames)}
                </span>
              </li>
            ))}
            {allowed.length > MAX_TOOL_LINES ? (
              <li className="text-xs text-faint">
                and {allowed.length - MAX_TOOL_LINES} more
              </li>
            ) : null}
          </ul>
        )}
        <Link
          href={`/agents/${agentId}`}
          className="inline-flex items-center gap-1 text-xs text-accent-strong hover:underline"
        >
          Manage tools and access <ExternalLink size={12} aria-hidden />
        </Link>
      </Row>
    </>
  );
}
