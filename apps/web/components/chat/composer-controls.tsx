"use client";

/**
 * The controls that live on the chat bar itself: which model the agent runs
 * on, how cautious it is before it acts, what it can currently reach, and
 * what this conversation has cost so far.
 *
 * They sit next to the box you type in because these are the things people
 * change mid-conversation — swapping to a cheaper model for a quick question,
 * letting an agent off the leash for a batch of work, handing it web access
 * because the answer isn't in the room. Each chip opens its menu *upward*, so
 * opening one never shifts the transcript or covers the composer.
 *
 * Everything here is readable by anyone who can open the chat. The mutations
 * are admin-only, mirroring what the API enforces (`PATCH /agents/{id}`,
 * `PUT /agents/{id}/policy`, and the grant routes), and non-admins get the
 * current value with a plain reason instead of a dead control.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, Coins, Cpu, ExternalLink, ShieldCheck, Wrench } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { PRESET_FRIENDLY, describeGrant, policySummary } from "@/components/company/agent-helpers";
import { ErrorNote, focusRing } from "@/components/ui";
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
import { capabilitySummary } from "@/lib/models";
import type { ApprovalPreset, ModelProfile } from "@/lib/types";
import {
  isPresetGranted,
  presetGrantsToAdd,
  presetGrantsToRevoke,
  presetMissingTools,
  presetScopeGaps,
  TOOL_PRESETS,
  type ToolPreset,
} from "@/lib/wizard";

const PRESETS: ApprovalPreset[] = ["autonomous", "balanced", "restricted"];

/** Chip labels: short enough for the bar, plain enough to need no glossary. */
const PRESET_LABELS: Record<ApprovalPreset, string> = {
  autonomous: "Free rein",
  balanced: "Balanced",
  restricted: "Careful",
};

/** Shown in place of a control when the viewer can't change something. */
const ADMIN_ONLY = "Only admins can change this.";

const MAX_TOOL_LINES = 4;

/** What this conversation has cost. Absent on a chat that doesn't exist yet. */
export interface ChatUsage {
  inputTokens: number;
  outputTokens: number;
  costMicros: number;
}

/** Escape or a click outside closes the menu; it is never modal, so the
 * transcript keeps scrolling behind it. Owned by each chip rather than by the
 * menu shell, so a chip can keep its menu open until its change has actually
 * been saved — and show the failure in place when it has not. */
function useMenu() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
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
  return { open, setOpen, wrapRef };
}

type MenuState = ReturnType<typeof useMenu>;

function Menu({
  menu,
  testId,
  icon: Icon,
  label,
  value,
  align = "left",
  children,
}: {
  menu: MenuState;
  testId: string;
  icon: typeof Cpu;
  /** What the chip controls, e.g. "Model" — read out before its value. */
  label: string;
  /** The current setting, shown on the chip. */
  value: string;
  align?: "left" | "right";
  children: React.ReactNode;
}) {
  const { open, setOpen, wrapRef } = menu;
  const name = `${label}: ${value}`;
  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        data-testid={`${testId}-trigger`}
        aria-label={name}
        title={name}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className={`inline-flex h-8 max-w-full items-center gap-1.5 rounded-lg px-2 text-xs font-medium transition-colors ${
          open ? "bg-accent-soft text-accent-strong" : "text-dim hover:bg-hover hover:text-ink"
        } ${focusRing}`}
      >
        <Icon size={13} aria-hidden className="shrink-0" />
        <span className="max-w-[9rem] truncate">{value}</span>
        <ChevronDown size={12} aria-hidden className="shrink-0 opacity-60" />
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label={label}
          data-testid={`${testId}-panel`}
          className={`absolute bottom-full z-40 mb-2 max-h-[min(26rem,60vh)] w-[19rem] max-w-[calc(100vw-2rem)] space-y-1 overflow-y-auto overscroll-contain rounded-2xl border border-line bg-surface p-2 text-sm shadow-card ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

function MenuTitle({ children }: { children: React.ReactNode }) {
  return (
    <h4 className="px-2 pt-1 text-[11px] font-medium uppercase tracking-wider text-faint">
      {children}
    </h4>
  );
}

function MenuNote({ children }: { children: React.ReactNode }) {
  return <p className="px-2 pb-1 text-xs text-dim">{children}</p>;
}

/** One pickable setting. `selected` is announced, not just coloured. */
function Option({
  selected,
  label,
  description,
  onClick,
  disabled = false,
  title,
  testId,
}: {
  selected: boolean;
  label: string;
  description?: string | null;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  testId?: string;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-pressed={selected}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={`flex w-full items-start gap-2 rounded-xl px-2 py-1.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        selected ? "bg-accent-soft" : "hover:bg-hover"
      } ${focusRing}`}
    >
      <span
        aria-hidden
        className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border ${
          selected ? "border-accent bg-accent text-white" : "border-line-strong"
        }`}
      >
        {selected ? <Check size={11} strokeWidth={3} /> : null}
      </span>
      <span className="min-w-0">
        <span
          className={`block text-[13px] font-medium ${selected ? "text-accent-strong" : "text-ink"}`}
        >
          {label}
        </span>
        {description ? (
          <span className="block text-xs leading-snug text-dim">{description}</span>
        ) : null}
      </span>
    </button>
  );
}

function describeModel(profile: ModelProfile): string | null {
  return capabilitySummary(profile) || null;
}

export function ChatComposerControls({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
  usage = null,
}: {
  workspaceId: string;
  /** null when the agent has left the workspace, or none is chosen yet. */
  agentId: string | null;
  agentName: string;
  /** Workspace role is admin or owner — the floor the API enforces for the
   * model, mode and grant changes offered here. */
  isAdmin: boolean;
  usage?: ChatUsage | null;
}) {
  return (
    <div
      data-testid="composer-control-chips"
      className="flex min-w-0 flex-wrap items-center gap-0.5"
    >
      {agentId ? (
        <>
          <ModelChip
            workspaceId={workspaceId}
            agentId={agentId}
            agentName={agentName}
            isAdmin={isAdmin}
          />
          <ModeChip
            workspaceId={workspaceId}
            agentId={agentId}
            agentName={agentName}
            isAdmin={isAdmin}
          />
          <ToolsChip workspaceId={workspaceId} agentId={agentId} isAdmin={isAdmin} />
        </>
      ) : null}
      {usage ? <UsageChip usage={usage} /> : null}
    </div>
  );
}

function ModelChip({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  isAdmin: boolean;
}) {
  const menu = useMenu();
  const agent = useAgent(workspaceId, agentId);
  const profiles = useModelProfiles(workspaceId);
  const queryClient = useQueryClient();
  const invalidateAccess = useInvalidateAgentAccess(workspaceId, agentId);
  const [error, setError] = useState<string | null>(null);

  const currentProfileId = agent.data?.model_profile_id ?? null;
  const currentProfile = (profiles.data ?? []).find((profile) => profile.id === currentProfileId);
  const modelLabel = currentProfile
    ? currentProfile.display_name
    : currentProfileId
      ? "A model that is no longer set up"
      : "Workspace default";

  const setModel = useMutation({
    mutationFn: (modelProfileId: string | null) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`, {
        method: "PATCH",
        body: { model_profile_id: modelProfileId },
      }),
    onSuccess: () => {
      setError(null);
      menu.setOpen(false);
      invalidateAccess();
      void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
    },
    onError: (mutationError) =>
      setError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Couldn't change the model. Try again.",
      ),
  });

  return (
    <Menu menu={menu} testId="composer-model" icon={Cpu} label="Model" value={modelLabel}>
      <MenuTitle>Model</MenuTitle>
      <ErrorNote message={error} />
      {isAdmin ? (
        <>
          <Option
            testId="composer-model-default"
            selected={currentProfileId === null}
            label="Workspace default"
            description="Whatever this workspace is set to use"
            disabled={setModel.isPending}
            onClick={() => setModel.mutate(null)}
          />
          {(profiles.data ?? []).map((profile) => (
            <Option
              key={profile.id}
              testId={`composer-model-${profile.id}`}
              selected={profile.id === currentProfileId}
              label={profile.display_name}
              description={describeModel(profile)}
              disabled={setModel.isPending}
              onClick={() => setModel.mutate(profile.id)}
            />
          ))}
          <MenuNote>Applies to {agentName} everywhere, from the next turn.</MenuNote>
        </>
      ) : (
        <MenuNote>
          {ADMIN_ONLY} {agentName} runs on {modelLabel}.
        </MenuNote>
      )}
    </Menu>
  );
}

function ModeChip({
  workspaceId,
  agentId,
  agentName,
  isAdmin,
}: {
  workspaceId: string;
  agentId: string;
  agentName: string;
  isAdmin: boolean;
}) {
  const menu = useMenu();
  const policy = useAgentPolicy(workspaceId, agentId);
  const invalidateAccess = useInvalidateAgentAccess(workspaceId, agentId);
  const [error, setError] = useState<string | null>(null);

  const currentPreset = policy.data?.preset ?? null;
  const chipValue = currentPreset ? PRESET_LABELS[currentPreset] : "Custom";

  const setPreset = useMutation({
    mutationFn: (preset: ApprovalPreset) =>
      api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/policy`, {
        method: "PUT",
        body: { preset },
      }),
    onSuccess: () => {
      setError(null);
      menu.setOpen(false);
      invalidateAccess();
    },
    onError: (mutationError) =>
      setError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Couldn't change the mode. Try again.",
      ),
  });

  return (
    <Menu menu={menu} testId="composer-mode" icon={ShieldCheck} label="Mode" value={chipValue}>
      <MenuTitle>Mode</MenuTitle>
      <ErrorNote message={error} />
      {isAdmin ? (
        <>
          {PRESETS.map((preset) => (
            <Option
              key={preset}
              testId={`composer-mode-${preset}`}
              selected={currentPreset === preset}
              label={PRESET_LABELS[preset]}
              description={PRESET_FRIENDLY[preset]}
              disabled={setPreset.isPending}
              onClick={() => setPreset.mutate(preset)}
            />
          ))}
          <MenuNote>How {agentName} behaves everywhere, from the next turn.</MenuNote>
        </>
      ) : (
        <MenuNote>
          {ADMIN_ONLY} {policySummary(policy.data)}.
        </MenuNote>
      )}
    </Menu>
  );
}

function ToolsChip({
  workspaceId,
  agentId,
  isAdmin,
}: {
  workspaceId: string;
  agentId: string;
  isAdmin: boolean;
}) {
  const menu = useMenu();
  const grants = useAgentGrants(workspaceId, agentId);
  const tools = useTools(workspaceId);
  // Connections only sharpen a grant's wording and fill in a preset's
  // connection id, and the endpoint is admin-only — skip it for everyone else.
  const connections = useConnections(workspaceId, isAdmin);
  const invalidateAccess = useInvalidateAgentAccess(workspaceId, agentId);
  const [error, setError] = useState<string | null>(null);

  const connectionNames = useMemo(
    () =>
      Object.fromEntries(
        (connections.data ?? []).map((connection) => [connection.id, connection.name]),
      ),
    [connections.data],
  );

  const toolList = tools.data ?? [];
  const grantList = grants.data ?? [];
  const allowed = grantList.filter((grant) => grant.effect === "allow");

  /** One capability on or off: add the grants it needs, or revoke the grants
   * it owns that no other capability still switched on needs. Same shape as
   * the agent's own Tools & Access tab, so the two can't disagree. */
  const toggleCapability = useMutation({
    mutationFn: async (preset: ToolPreset) => {
      if (isPresetGranted(grantList, preset, toolList)) {
        const keep = TOOL_PRESETS.filter(
          (other) => other.id !== preset.id && isPresetGranted(grantList, other, toolList),
        );
        for (const grant of presetGrantsToRevoke(grantList, preset, toolList, keep)) {
          await api<void>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/grants/${grant.id}`, {
            method: "DELETE",
          });
        }
        return;
      }
      for (const body of presetGrantsToAdd(grantList, preset, toolList, connections.data ?? [])) {
        await api(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/grants`, {
          method: "POST",
          body,
        });
      }
    },
    onSuccess: () => {
      setError(null);
      invalidateAccess();
    },
    onError: (mutationError) =>
      setError(
        mutationError instanceof ApiError
          ? mutationError.detail
          : "Couldn't change what this agent can reach. Try again.",
      ),
  });

  const loading = grants.isPending || tools.isPending;
  const chipValue = loading
    ? "Tools"
    : allowed.length === 0
      ? "No tools"
      : allowed.length === 1
        ? "1 tool"
        : `${allowed.length} tools`;

  return (
    <Menu menu={menu} testId="composer-tools" icon={Wrench} label="Tools" value={chipValue}>
      <MenuTitle>Tools and access</MenuTitle>
      <ErrorNote message={error} />
      {loading ? (
        <MenuNote>Loading…</MenuNote>
      ) : (
        <>
          {isAdmin
            ? TOOL_PRESETS.map((preset) => {
                const granted = isPresetGranted(grantList, preset, toolList);
                const missing = presetMissingTools(preset, toolList);
                const gaps = granted
                  ? []
                  : presetScopeGaps(preset, toolList, connections.data ?? []);
                const unavailable = missing.length === Object.keys(preset.tools).length;
                const blocked = !granted && gaps.length > 0;
                return (
                  <Option
                    key={preset.id}
                    testId={`composer-tools-${preset.id}`}
                    selected={granted}
                    label={preset.label}
                    description={preset.summary}
                    disabled={unavailable || blocked || toggleCapability.isPending}
                    title={
                      unavailable
                        ? "This workspace has no connector for this yet."
                        : blocked
                          ? `Needs a connection first: ${gaps.join(", ")}`
                          : preset.description
                    }
                    onClick={() => toggleCapability.mutate(preset)}
                  />
                );
              })
            : null}
          {allowed.length === 0 ? (
            <MenuNote>
              Nothing yet{isAdmin ? "" : " — ask an admin to give it some tools"}.
            </MenuNote>
          ) : (
            <>
              <MenuTitle>Can reach now</MenuTitle>
              <ul className="space-y-1 px-2 pb-1 text-[13px] text-ink">
                {allowed.slice(0, MAX_TOOL_LINES).map((grant) => (
                  <li key={grant.id} className="flex items-start gap-2">
                    <span aria-hidden className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-ok" />
                    <span className="min-w-0">
                      {describeGrant(grant, toolList, connectionNames)}
                    </span>
                  </li>
                ))}
                {allowed.length > MAX_TOOL_LINES ? (
                  <li className="text-xs text-faint">
                    and {allowed.length - MAX_TOOL_LINES} more
                  </li>
                ) : null}
              </ul>
            </>
          )}
          <Link
            href={`/agents/${agentId}`}
            className="inline-flex items-center gap-1 px-2 pb-1 text-xs text-accent-strong hover:underline"
          >
            Manage tools and access <ExternalLink size={12} aria-hidden />
          </Link>
        </>
      )}
    </Menu>
  );
}

function UsageChip({ usage }: { usage: ChatUsage }) {
  const menu = useMenu();
  const total = usage.inputTokens + usage.outputTokens;
  return (
    <Menu
      menu={menu}
      testId="composer-usage"
      icon={Coins}
      label="This chat"
      value={`${formatTokens(total)} · ${formatCostMicros(usage.costMicros)}`}
      align="right"
    >
      <MenuTitle>This chat</MenuTitle>
      <p data-testid="composer-usage-total" className="px-2 tabular-nums text-ink">
        {formatTokens(total)} tokens · {formatCostMicros(usage.costMicros)}
      </p>
      <p className="px-2 text-xs text-faint">
        {formatTokens(usage.inputTokens)} in · {formatTokens(usage.outputTokens)} out
      </p>
      <MenuNote>
        Counted across every message and tool call in this chat, at the prices set for the models
        it used.
      </MenuNote>
    </Menu>
  );
}
