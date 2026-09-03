"use client";

/** Agent profile "Persona" tab: which card this agent wears, the picker to
 * change it, and the block it will read. A persona takes effect on the
 * agent's next run, never mid-run, and the tab says so. Admin-only editing;
 * everyone can look. */

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { LoadError, SectionCard } from "@/components/company/bits";
import {
  PersonaSourceBadge,
  PersonaTagBadges,
  SwitchedOffBadge,
} from "@/components/personas/persona-badges";
import { PersonaBlockPreview } from "@/components/personas/persona-block-preview";
import { PersonaPicker } from "@/components/personas/persona-picker";
import { Button, ErrorNote, Spinner } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useInvalidateOrg, useInvalidatePersonas, usePersonas } from "@/lib/hooks";
import type { Agent } from "@/lib/types";

export function PersonaPanel({
  workspaceId,
  agent,
  isAdmin,
}: {
  workspaceId: string;
  agent: Agent;
  isAdmin: boolean;
}) {
  const personas = usePersonas(workspaceId);
  const invalidatePersonas = useInvalidatePersonas(workspaceId);
  const invalidateOrg = useInvalidateOrg(workspaceId);
  const [error, setError] = useState<string | null>(null);

  const assign = useMutation({
    mutationFn: (personaId: string | null) =>
      api<Agent>(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}`, {
        method: "PATCH",
        body: { persona_id: personaId },
      }),
    onSuccess: () => {
      setError(null);
      invalidatePersonas();
      invalidateOrg();
    },
    onError: (mutationError) => setError(errorText(mutationError, "Changing the persona failed.")),
  });

  if (personas.isPending) return <Spinner label="Loading personas…" />;
  if (personas.isError || !personas.data) {
    return <LoadError what="the personas library" onRetry={() => void personas.refetch()} />;
  }

  const items = personas.data.items;
  const worn = agent.persona ?? null;
  const card = worn ? items.find((persona) => persona.id === worn.id) : undefined;

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <SectionCard
        title="Persona"
        description={`How ${agent.name} sounds — voice, pace, manner. It never changes what the agent may do.`}
        className="lg:col-span-2"
      >
        <div data-testid="current-persona" className="rounded-xl border border-line bg-raised px-3 py-2.5">
          {!worn ? (
            <p className="text-sm text-dim">No persona. {agent.name} speaks in its own default way.</p>
          ) : !worn.enabled ? (
            <div className="space-y-1.5">
              <SwitchedOffBadge />
              <p className="text-sm text-dim">
                {worn.display_name} is switched off in the library, so {agent.name} runs without a
                persona until it’s turned back on.
              </p>
              <Link href="/personas" className="text-sm text-accent-strong hover:underline">
                Open the Personas page
              </Link>
            </div>
          ) : (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{worn.display_name}</span>
                {card ? <PersonaSourceBadge source={card.source} /> : null}
                <PersonaTagBadges tags={worn.tags} />
              </div>
              {card ? <p className="text-sm text-dim">{card.description}</p> : null}
            </div>
          )}
        </div>
        {isAdmin ? (
          <div className="mt-4 space-y-3">
            {worn ? (
              <Button size="sm" onClick={() => assign.mutate(null)} disabled={assign.isPending}>
                Clear persona
              </Button>
            ) : null}
            <PersonaPicker
              personas={items}
              value={worn?.id ?? null}
              onChange={(id) => assign.mutate(id)}
              disabled={assign.isPending}
            />
            <p className="text-xs text-faint">
              Takes effect on {agent.name}’s next run, never in the middle of one.
            </p>
            <ErrorNote message={error} />
          </div>
        ) : (
          <p className="mt-3 text-xs text-faint">Only admins can change which persona an agent wears.</p>
        )}
      </SectionCard>
      <SectionCard title={`What ${agent.name} reads`}>
        {card ? (
          <div className="space-y-2">
            {!card.enabled ? (
              <p className="text-sm text-dim">Not rendered while the persona is switched off.</p>
            ) : null}
            <PersonaBlockPreview
              name={card.name}
              displayName={card.display_name}
              facets={card.facets}
              audience="both"
            />
          </div>
        ) : (
          <p className="text-sm text-dim">
            Without a persona, {agent.name} gets no “How you work” block — only its own
            instructions.
          </p>
        )}
      </SectionCard>
    </div>
  );
}
