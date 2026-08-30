"use client";

/** First-run guidance for a workspace that is not set up yet. An agent
 * cannot work without a model, so the provider step comes first and is
 * marked done once any model profile exists. Home passes `includeApps` to
 * add the optional "connect an app" step; the Chats empty state does not. */

import { CheckCircle2, Circle } from "lucide-react";
import { ButtonLink } from "@/components/ui";
import { useAgents, useConnections, useConversations, useModelProfiles } from "@/lib/hooks";

export interface SetupStatus {
  isPending: boolean;
  hasModel: boolean;
  hasAgents: boolean;
  hasApps: boolean;
  /** Whether anyone has started a chat yet. Always false unless the caller
   * asked for it with `includeChats`; the checklist does not need it. */
  hasChats: boolean;
  /** A model, at least one active agent, and at least one connected app. */
  complete: boolean;
}

/** Whether the workspace still needs first-run setup. Connections are an
 * admin-only endpoint, so `hasApps` is only meaningful for admins.
 *
 * `includeChats` is opt-in because only the guided introduction asks whether a
 * first chat exists, and the checklist on three other screens should not pay
 * for a conversation fetch it never reads. */
export function useSetupStatus(
  workspaceId: string,
  isAdmin: boolean,
  { includeChats = false }: { includeChats?: boolean } = {},
): SetupStatus {
  const profiles = useModelProfiles(workspaceId);
  const agents = useAgents(workspaceId);
  const connections = useConnections(workspaceId, isAdmin);
  const conversations = useConversations(workspaceId, { limit: 1 }, includeChats);

  const hasModel = (profiles.data?.length ?? 0) > 0;
  const hasAgents = (agents.data ?? []).some((agent) => agent.status === "active");
  const hasApps = (connections.data?.length ?? 0) > 0;
  const hasChats = includeChats && (conversations.data?.items.length ?? 0) > 0;
  const isPending =
    profiles.isPending ||
    agents.isPending ||
    (isAdmin && connections.isPending) ||
    (includeChats && conversations.isPending);

  return {
    isPending,
    hasModel,
    hasAgents,
    hasApps,
    hasChats,
    complete: hasModel && hasAgents && (!isAdmin || hasApps),
  };
}

export function FirstRunSteps({
  workspaceId,
  isAdmin,
  includeApps = false,
}: {
  workspaceId: string;
  isAdmin: boolean;
  includeApps?: boolean;
}) {
  const status = useSetupStatus(workspaceId, isAdmin);

  if (!isAdmin) {
    return <p className="text-xs text-faint">Ask a workspace admin to add one.</p>;
  }

  const steps = [
    {
      done: status.hasModel,
      title: "Connect a model provider",
      detail: status.hasModel
        ? "A model is ready for your agents to use."
        : "Agents think with a model — add OpenAI, Anthropic, or any compatible endpoint.",
      href: "/models",
      cta: "Set up a model",
    },
    {
      done: status.hasAgents,
      title: "Create your first agent",
      detail: status.hasAgents
        ? "Your first agent is ready — start a chat any time."
        : "Give it a name and a role, then start a chat.",
      href: "/agents/new",
      cta: "Create an agent",
    },
    ...(includeApps
      ? [
          {
            done: status.hasApps,
            title: "Connect an app",
            detail: status.hasApps
              ? "Your agents can reach an outside app."
              : "Optional, but agents get far more done with GitHub, Notion, Slack, or any MCP server.",
            href: "/apps",
            cta: "Browse apps",
          },
        ]
      : []),
  ];
  const current = steps.find((step) => !step.done) ?? steps[steps.length - 1];

  return (
    <div className="w-full max-w-md space-y-3 text-left">
      <ol className="space-y-2">
        {steps.map((step, index) => {
          const active = step === current;
          return (
            <li
              key={step.href}
              data-testid={`setup-step-${index + 1}`}
              className={`flex items-start gap-3 rounded-xl border px-3.5 py-3 ${
                active ? "border-accent bg-accent-soft/60" : "border-line bg-raised"
              }`}
            >
              {step.done ? (
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-ok" aria-label="Done" />
              ) : (
                <Circle size={18} className="mt-0.5 shrink-0 text-faint" aria-hidden />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink">
                  <span className="text-faint">{index + 1}.</span> {step.title}
                </p>
                <p className="text-xs text-dim">{step.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="flex justify-center">
        <ButtonLink href={current.href} variant="primary">
          {current.cta}
        </ButtonLink>
      </div>
    </div>
  );
}
