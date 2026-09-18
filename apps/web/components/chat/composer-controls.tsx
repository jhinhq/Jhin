"use client";

/**
 * The controls that sit on the composer's own row, beside Send: which model
 * the agent runs on, how cautious it is before acting, what it can reach, and
 * what this chat has cost so far. They are where a person already is when the
 * answer makes them want to change one of those things.
 *
 * Each chip owns a popover that opens *upward*, because the composer is
 * pinned to the bottom of the thread; only one is open at a time, and opening
 * one never moves the transcript or the field.
 *
 * Everything here is readable by anyone who can open the chat. The writes are
 * admin-only — the API enforces admin on `PATCH /agents/{id}`,
 * `PUT /agents/{id}/policy` and the bundle endpoints — and non-admins see the
 * current value with a plain reason instead of a control.
 *
 * Scope, said plainly in the UI too: these change the *agent*, everywhere,
 * from its next turn. This chat has no settings of its own.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  Coins,
  Cpu,
  ExternalLink,
  Minus,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { PRESET_FRIENDLY, describeGrant, policySummary } from "@/components/company/agent-helpers";
import { ErrorNote, focusRing } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { toggleOrganizationBundle } from "@/lib/bundle-actions";
import { isConnectorBundle } from "@/lib/bundles";
import { formatCostMicros, formatTokens } from "@/lib/format";
import {
  useAgent,
  useAgentBundles,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useInvalidateAgentAccess,
  useModelProfiles,
  useTools,
} from "@/lib/hooks";
import { describeRule, keptRules } from "@/lib/policy";
import type {
  ApprovalPreset,
  BundleRemoveOut,
  BundleStatusOut,
  ConversationDetail,
} from "@/lib/types";
import { TOOL_PRESETS } from "@/lib/wizard";

const PRESETS: ApprovalPreset[] = ["autonomous", "balanced", "restricted"];

const PRESET_LABELS: Record<ApprovalPreset, string> = {
  autonomous: "Free rein",
  balanced: "Balanced",
  restricted: "Careful",
};

/** Shown in place of a control when the viewer can't change something. */
const ADMIN_ONLY = "Only admins can change this.";

/** Hand-picked grants listed under the bundles before the list is summarised. */
const MAX_GRANT_LINES = 3;

type ControlId = "model" | "mode" | "tools" | "usage";

function Chip({
  testId,
  label,
  value,
  icon,
  open,
  onToggle,
  panelLabel,
  children,
}: {
  testId: string;
  /** Accessible name prefix, e.g. "Model" — the chip reads "Model: Sonnet". */
  label: string;
  value: string;
  icon: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  panelLabel: string;
  children: React.ReactNode;
}) {
  // No positioning of its own: the panel anchors to the control bar, so a
  // chip near the right edge can't push its popover off the screen.
  return (
    <div>
      <button
        type="button"
        data-testid={`${testId}-chip`}
        aria-label={`${label}: ${value}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={`${label}: ${value}`}
        onClick={onToggle}
        className={`inline-flex h-9 max-w-[13rem] items-center gap-1.5 rounded-lg px-2 text-xs font-medium transition-colors md:h-8 ${
          open ? "bg-accent-soft text-accent-strong" : "text-dim hover:bg-hover hover:text-ink"
        } ${focusRing}`}
      >
        <span aria-hidden className="shrink-0">
          {icon}
        </span>
        <span className="truncate">{value}</span>
        <ChevronDown size={12} aria-hidden className="shrink-0 opacity-60" />
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label={panelLabel}
          data-testid={`${testId}-panel`}
          className="absolute bottom-full left-0 z-40 mb-1.5 max-h-[min(26rem,60dvh)] w-[19rem] max-w-[calc(100vw-2rem)] space-y-2 overflow-y-auto overscroll-contain rounded-2xl border border-line bg-surface p-3 text-sm shadow-card"
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

/** One row in a popover's pick-list: a check when it is the current value. */
function Option({
  testId,
  title,
  detail,
  selected,
  disabled,
  onSelect,
}: {
  testId: string;
  title: string;
  detail?: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onSelect}
      className={`flex w-full items-start gap-2 rounded-xl border px-2.5 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        selected ? "border-accent bg-accent-soft" : "border-transparent hover:bg-hover"
      } ${focusRing}`}
    >
      <span
        aria-hidden
        className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
          selected ? "border-accent bg-accent text-white" : "border-line-strong"
        }`}
      >
        {selected ? <Check size={10} strokeWidth={3} /> : null}
      </span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-ink">{title}</span>
        {detail ? <span className="mt-0.5 block text-xs leading-snug text-dim">{detail}</span> : null}
      </span>
    </button>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="px-1 text-xs leading-snug text-dim">{children}</p>;
}

export function ChatComposerControls({
  workspaceId,
  detail,
  isAdmin,
}: {
  workspaceId: string;
  detail: ConversationDetail;
  /** Workspace role is admin or owner — the floor the API enforces for the
   * model, policy and bundle writes. */
  isAdmin: boolean;
}) {
  const agentId = detail.agent?.id ?? null;
  const agentName = detail.agent?.name ?? detail.conversation.agent_name ?? "this agent";
  const [open, setOpen] = useState<ControlId | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on Escape or a click outside. The popovers are not modal, so the
  // transcript stays scrollable and the field stays typeable behind them.
  useEffect(() => {
    if (open === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(null);
    };
    const onDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(null);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const toggle = (id: ControlId) => {
    setError(null);
    setOpen((current) => (current === id ? null : id));
  };

  return (
    <div
      ref={wrapRef}
      data-testid="composer-controls-bar"
      role="group"
      aria-label={`Settings for ${agentName}`}
      className="relative flex min-w-0 flex-wrap items-center gap-0.5"
    >
      {agentId ? (
        <AgentControls
          workspaceId={workspaceId}
          agentId={agentId}
          agentName={agentName}
          isAdmin={isAdmin}
          open={open}
          onToggle={toggle}
          onClose={() => setOpen(null)}
          error={error}
          onError={setError}
        />
      ) : null}
      <UsageChip detail={detail} open={open === "usage"} onToggle={() => toggle("usage")} />
    </div>
  );
}

function UsageChip({
  detail,
  open,
  onToggle,
}: {
  detail: ConversationDetail;
  open: boolean;
  onToggle: () => void;
}) {
  const tokens = detail.total_input_tokens + detail.total_output_tokens;
  return (
    <Chip
      testId="composer-usage"
      label="This chat"
      value={`${formatTokens(tokens)} · ${formatCostMicros(detail.total_cost_micros)}`}
      icon={<Coins size={13} />}
      open={open}
      onToggle={onToggle}
      panelLabel="What this chat has used"
    >
      <p className="px-1 text-[13px] font-medium text-ink">What this chat has used</p>
      <p data-testid="composer-usage-total" className="px-1 tabular-nums text-ink">
        {formatTokens(tokens)} tokens · {formatCostMicros(detail.total_cost_micros)}
      </p>
      <p className="px-1 text-xs tabular-nums text-faint">
        {formatTokens(detail.total_input_tokens)} in · {formatTokens(detail.total_output_tokens)} out
      </p>
      <Note>Every turn in this chat, including work the agent did on its own.</Note>
    </Chip>
  );
}

function AgentControls({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
  open,
  onToggle,
  onClose,
  error,
  onError,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  isAdmin: boolean;
  open: ControlId | null;
  onToggle: (id: ControlId) => void;
  onClose: () => void;
  error: string | null;
  onError: (message: string | null) => void;
}) {
  const agent = useAgent(workspaceId, agentId);
  const profiles = useModelProfiles(workspaceId);
  const policy = useAgentPolicy(workspaceId, agentId);
  const grants = useAgentGrants(workspaceId, agentId);
  const tools = useTools(workspaceId);
  const bundles = useAgentBundles(workspaceId, agentId);
  // Connection names only sharpen a grant's wording, and the endpoint is
  // admin-only — skip the request for everyone else.
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

  const failed = (mutationError: unknown, fallback: string) =>
    onError(mutationError instanceof ApiError ? mutationError.detail : fallback);

  const setModel = useMutation({
    mutationFn: (modelProfileId: string | null) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`, {
        method: "PATCH",
        body: { model_profile_id: modelProfileId },
      }),
    onSuccess: () => {
      onError(null);
      invalidateAgent();
      onClose();
    },
    onError: (mutationError) => failed(mutationError, "Couldn't change the model. Try again."),
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
      onClose();
    },
    onError: (mutationError) => failed(mutationError, "Couldn't change the mode. Try again."),
  });

  const currentProfileId = agent.data?.model_profile_id ?? null;
  const currentProfile = (profiles.data ?? []).find((profile) => profile.id === currentProfileId);
  const modelLabel = currentProfile
    ? currentProfile.display_name
    : currentProfileId
      ? "Model no longer set up"
      : "Default model";

  const currentPreset = policy.data?.preset ?? null;
  const modeLabel = currentPreset ? PRESET_LABELS[currentPreset] : "Custom mode";
  const kept = keptRules(policy.data?.rules ?? []);

  return (
    <>
      <Chip
        testId="composer-model"
        label="Model"
        value={modelLabel}
        icon={<Cpu size={13} />}
        open={open === "model"}
        onToggle={() => onToggle("model")}
        panelLabel={`Model for ${agentName}`}
      >
        <ErrorNote message={error} />
        <p className="px-1 text-[13px] font-medium text-ink">Model</p>
        {isAdmin ? (
          <>
            <div role="radiogroup" aria-label="Model" className="space-y-0.5">
              <Option
                testId="composer-model-default"
                title="Workspace default"
                selected={currentProfileId === null}
                disabled={setModel.isPending}
                onSelect={() => setModel.mutate(null)}
              />
              {(profiles.data ?? []).map((profile) => (
                <Option
                  key={profile.id}
                  testId={`composer-model-${profile.id}`}
                  title={profile.display_name}
                  detail={profile.model_name}
                  selected={profile.id === currentProfileId}
                  disabled={setModel.isPending}
                  onSelect={() => setModel.mutate(profile.id)}
                />
              ))}
            </div>
            {profiles.isPending ? <Note>Loading models…</Note> : null}
            <Note>Applies to {agentName} everywhere, from its next turn.</Note>
          </>
        ) : (
          <>
            <p data-testid="composer-model-readonly" className="px-1 text-ink">
              {modelLabel}
            </p>
            <Note>{ADMIN_ONLY}</Note>
          </>
        )}
      </Chip>

      <Chip
        testId="composer-mode"
        label="Mode"
        value={modeLabel}
        icon={<ShieldCheck size={13} />}
        open={open === "mode"}
        onToggle={() => onToggle("mode")}
        panelLabel={`Mode for ${agentName}`}
      >
        <ErrorNote message={error} />
        <p className="px-1 text-[13px] font-medium text-ink">
          What {agentName} does before it acts
        </p>
        {isAdmin ? (
          <>
            <div role="radiogroup" aria-label="Mode" className="space-y-0.5">
              {PRESETS.map((preset) => (
                <Option
                  key={preset}
                  testId={`composer-mode-${preset}`}
                  title={PRESET_LABELS[preset]}
                  detail={PRESET_FRIENDLY[preset]}
                  selected={currentPreset === preset}
                  disabled={setPreset.isPending}
                  onSelect={() => setPreset.mutate(preset)}
                />
              ))}
            </div>
            {kept.length > 0 ? (
              <Note>
                <span data-testid="composer-mode-kept">
                  Whichever mode you pick, {kept.map((rule) => describeRule(rule)).join("; ")}.
                </span>
              </Note>
            ) : null}
            <Note>Applies to {agentName} everywhere, from its next turn.</Note>
          </>
        ) : (
          <>
            <p data-testid="composer-mode-readonly" className="px-1 text-ink">
              {policySummary(policy.data)}
            </p>
            <Note>{ADMIN_ONLY}</Note>
          </>
        )}
      </Chip>

      <ToolsChip
        workspaceId={workspaceId}
        agentId={agentId}
        agentName={agentName}
        isAdmin={isAdmin}
        open={open === "tools"}
        onToggle={() => onToggle("tools")}
        error={error}
        onError={onError}
        onChanged={invalidateAgent}
        bundles={bundles}
        grants={grants}
        tools={tools}
        connections={connections}
        policy={policy}
        connectionNames={connectionNames}
      />
    </>
  );
}

function ToolsChip({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
  open,
  onToggle,
  error,
  onError,
  onChanged,
  bundles,
  grants,
  tools,
  connections,
  policy,
  connectionNames,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  isAdmin: boolean;
  open: boolean;
  onToggle: () => void;
  error: string | null;
  onError: (message: string | null) => void;
  onChanged: () => void;
  bundles: ReturnType<typeof useAgentBundles>;
  grants: ReturnType<typeof useAgentGrants>;
  tools: ReturnType<typeof useTools>;
  connections: ReturnType<typeof useConnections>;
  policy: ReturnType<typeof useAgentPolicy>;
  connectionNames: Record<string, string>;
}) {
  /** A connector bundle being turned off, with the dry run that says what
   * that revokes. Confirmed in the popover — turning access off is not
   * something to do on a single stray click. */
  const [turningOff, setTurningOff] = useState<{
    bundle: BundleStatusOut;
    preview: BundleRemoveOut;
  } | null>(null);
  /** A bundle the server could not write without an answer we can't ask for
   * here (which connection, which repositories). */
  const [needsSetup, setNeedsSetup] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const bundleList = (bundles.data ?? []).filter(
    (bundle) => bundle.readiness.state !== "unavailable" || bundle.state !== "off",
  );
  const on = bundleList.filter((bundle) => bundle.state === "on");
  const allowed = (grants.data ?? []).filter((grant) => grant.effect === "allow");
  const bundleCapabilities = new Set(
    on.flatMap((bundle) => bundle.tools.map((tool) => tool.capability)),
  );
  const handPicked = allowed.filter((grant) => !bundleCapabilities.has(grant.capability));

  const chipValue = bundles.isPending
    ? "Tools"
    : on.length === 0
      ? allowed.length === 0
        ? "No tools"
        : `${allowed.length} ${allowed.length === 1 ? "tool" : "tools"}`
      : on.length === 1
        ? on[0].label
        : `${on[0].label} +${on.length - 1}`;

  const applyBundle = useMutation({
    mutationFn: (bundleId: string) =>
      api<{ needs: unknown[] }>(
        `/api/v1/workspaces/${workspaceId}/agents/${agentId}/bundles/${bundleId}`,
        {
          method: "POST",
          body: { connections: {}, repositories: [], base: null, dry_run: false },
        },
      ),
  });

  const removeBundle = useMutation({
    mutationFn: ({ bundleId, dryRun }: { bundleId: string; dryRun: boolean }) =>
      api<BundleRemoveOut>(
        `/api/v1/workspaces/${workspaceId}/agents/${agentId}/bundles/${bundleId}`,
        { method: "DELETE", params: dryRun ? { dry_run: "true" } : {} },
      ),
  });

  const organizationBundle = useMutation({
    mutationFn: (bundle: BundleStatusOut) => {
      const preset = TOOL_PRESETS.find((candidate) => candidate.id === bundle.id);
      if (!preset) throw new Error(`No client-side preset for ${bundle.id}`);
      return toggleOrganizationBundle({
        workspaceId,
        agentId,
        preset,
        grants: grants.data ?? [],
        tools: tools.data ?? [],
        connections: connections.data ?? [],
        rules: policy.data?.rules ?? [],
      });
    },
  });

  const flip = async (bundle: BundleStatusOut) => {
    onError(null);
    setNeedsSetup(null);
    setTurningOff(null);
    setBusy(bundle.id);
    try {
      if (!isConnectorBundle(bundle.id)) {
        await organizationBundle.mutateAsync(bundle);
        onChanged();
      } else if (bundle.state === "on") {
        // Dry run first: the confirmation names what turning it off revokes.
        const preview = await removeBundle.mutateAsync({ bundleId: bundle.id, dryRun: true });
        setTurningOff({ bundle, preview });
      } else {
        const result = await applyBundle.mutateAsync(bundle.id);
        // The server writes nothing while a question is open; it hands the
        // question back instead, and those are answered on the agent's page.
        if (result.needs.length > 0) setNeedsSetup(bundle.id);
        else onChanged();
      }
    } catch (mutationError) {
      onError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : `Couldn't change ${bundle.label}. Try again.`,
      );
    } finally {
      setBusy(null);
    }
  };

  const confirmTurnOff = async () => {
    if (!turningOff) return;
    const { bundle } = turningOff;
    setBusy(bundle.id);
    try {
      await removeBundle.mutateAsync({ bundleId: bundle.id, dryRun: false });
      setTurningOff(null);
      onChanged();
    } catch (mutationError) {
      onError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : `Couldn't turn off ${bundle.label}. Try again.`,
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <Chip
      testId="composer-tools"
      label="Tools"
      value={chipValue}
      icon={<Wrench size={13} />}
      open={open}
      onToggle={onToggle}
      panelLabel={`What ${agentName} can use`}
    >
      <ErrorNote message={error} />
      <p className="px-1 text-[13px] font-medium text-ink">What {agentName} can use</p>
      {bundles.isPending || tools.isPending ? (
        <Note>Loading…</Note>
      ) : bundleList.length === 0 ? (
        <Note>This workspace has no capabilities to hand out yet.</Note>
      ) : (
        <ul className="space-y-0.5">
          {bundleList.map((bundle) => {
            const isOn = bundle.state === "on";
            const partial = bundle.state === "partial";
            const needs = !isOn && bundle.readiness.state === "needs";
            const working = busy === bundle.id;
            return (
              <li key={bundle.id}>
                <div
                  data-testid={`composer-bundle-${bundle.id}`}
                  data-state={bundle.state}
                  className="flex items-center gap-2 rounded-xl px-2.5 py-1.5"
                >
                  <span
                    aria-hidden
                    className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border ${
                      isOn
                        ? "border-accent bg-accent text-white"
                        : partial
                          ? "border-accent text-accent"
                          : "border-line-strong"
                    }`}
                  >
                    {isOn ? <Check size={10} strokeWidth={3} /> : null}
                    {partial ? <Minus size={10} strokeWidth={3} /> : null}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13px] text-ink" title={bundle.summary}>
                    {bundle.label}
                  </span>
                  {isAdmin ? (
                    needs ? (
                      <Link
                        href={`/agents/${agentId}?tab=access`}
                        data-testid={`composer-bundle-setup-${bundle.id}`}
                        className={`shrink-0 rounded-lg px-1.5 py-1 text-xs text-accent-strong hover:underline ${focusRing}`}
                      >
                        Set up
                      </Link>
                    ) : (
                      <button
                        type="button"
                        data-testid={`composer-bundle-toggle-${bundle.id}`}
                        aria-pressed={isOn}
                        aria-label={`${isOn ? "Turn off" : "Turn on"} ${bundle.label}`}
                        disabled={busy !== null}
                        onClick={() => void flip(bundle)}
                        className={`shrink-0 rounded-lg border px-2 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                          isOn
                            ? "border-line-strong text-dim hover:border-danger/40 hover:text-danger"
                            : "border-line-strong text-ink hover:border-accent"
                        } ${focusRing}`}
                      >
                        {working ? "…" : isOn ? "Turn off" : partial ? "Finish" : "Turn on"}
                      </button>
                    )
                  ) : (
                    <span className="shrink-0 text-xs text-faint">
                      {isOn ? "On" : partial ? "Partly on" : "Off"}
                    </span>
                  )}
                </div>
                {needsSetup === bundle.id ? (
                  <p
                    data-testid={`composer-bundle-needs-${bundle.id}`}
                    className="px-2.5 pb-1.5 text-xs text-warn"
                  >
                    {bundle.label} needs an app connected first.{" "}
                    <Link href={`/agents/${agentId}?tab=access`} className="underline">
                      Set it up
                    </Link>
                    .
                  </p>
                ) : null}
                {turningOff?.bundle.id === bundle.id ? (
                  <div
                    data-testid={`composer-bundle-confirm-${bundle.id}`}
                    className="mx-1 mb-1 rounded-xl border border-danger/30 bg-danger-soft px-2.5 py-2 text-xs text-danger"
                  >
                    <p>
                      Turn off {bundle.label}? That revokes {turningOff.preview.revoked.length}{" "}
                      {turningOff.preview.revoked.length === 1 ? "grant" : "grants"}
                      {turningOff.preview.hand_made.length > 0
                        ? `, including ${turningOff.preview.hand_made.length} added by hand`
                        : ""}
                      .
                    </p>
                    <div className="mt-1.5 flex gap-1.5">
                      <button
                        type="button"
                        data-testid={`composer-bundle-confirm-yes-${bundle.id}`}
                        disabled={busy !== null}
                        onClick={() => void confirmTurnOff()}
                        className={`rounded-lg border border-danger/40 px-2 py-1 font-medium hover:bg-danger/10 disabled:opacity-50 ${focusRing}`}
                      >
                        Turn it off
                      </button>
                      <button
                        type="button"
                        onClick={() => setTurningOff(null)}
                        className={`rounded-lg px-2 py-1 font-medium text-dim hover:text-ink ${focusRing}`}
                      >
                        Keep it
                      </button>
                    </div>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {handPicked.length > 0 ? (
        <div className="space-y-1 border-t border-line pt-2">
          <p className="px-1 text-xs font-medium uppercase tracking-wider text-faint">
            Also allowed
          </p>
          <ul className="space-y-1 px-1 text-[13px] text-ink">
            {handPicked.slice(0, MAX_GRANT_LINES).map((grant) => (
              <li key={grant.id} className="flex items-start gap-2">
                <span aria-hidden className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-ok" />
                <span className="min-w-0">
                  {describeGrant(grant, tools.data ?? [], connectionNames)}
                </span>
              </li>
            ))}
            {handPicked.length > MAX_GRANT_LINES ? (
              <li className="text-xs text-faint">
                and {handPicked.length - MAX_GRANT_LINES} more
              </li>
            ) : null}
          </ul>
        </div>
      ) : null}

      {isAdmin ? (
        <Note>Turning one on gives {agentName} those tools everywhere, not just here.</Note>
      ) : (
        <Note>{ADMIN_ONLY}</Note>
      )}
      <Link
        href={`/agents/${agentId}?tab=access`}
        className="inline-flex items-center gap-1 px-1 text-xs text-accent-strong hover:underline"
      >
        Manage tools and access <ExternalLink size={12} aria-hidden />
      </Link>
    </Chip>
  );
}
