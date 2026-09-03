"use client";

/** One card in the Personas gallery: who it is, how it sounds, who wears it,
 * and — for admins — what can be done with it. Built-ins get Duplicate
 * instead of Edit and never Delete. */

import { Copy, Pencil, Trash2 } from "lucide-react";
import {
  PersonaSourceBadge,
  PersonaTagBadges,
  SwitchedOffBadge,
} from "@/components/personas/persona-badges";
import { Button, focusRing } from "@/components/ui";
import { agentCountText } from "@/lib/personas";
import type { Persona } from "@/lib/types";

export function PersonaCard({
  persona,
  isAdmin,
  onOpen,
  onEdit,
  onDuplicate,
  onToggle,
  onDelete,
  toggling,
  duplicating,
  removing,
}: {
  persona: Persona;
  isAdmin: boolean;
  onOpen: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onToggle: () => void;
  onDelete: () => void;
  toggling: boolean;
  duplicating: boolean;
  removing: boolean;
}) {
  const name = persona.display_name;
  return (
    <li
      data-testid={`persona-${persona.name}`}
      className={`flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card ${
        persona.enabled ? "" : "opacity-70"
      }`}
    >
      <div className="space-y-1">
        <h3 className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onOpen}
            className={`rounded font-display text-sm font-semibold hover:underline ${focusRing}`}
          >
            {name}
          </button>
          <PersonaSourceBadge source={persona.source} />
          {!persona.enabled ? <SwitchedOffBadge /> : null}
        </h3>
        <p className="line-clamp-2 text-sm text-dim">{persona.description}</p>
      </div>
      <p className="line-clamp-2 text-[13px] italic text-ink/80">“{persona.facets.voice}”</p>
      <PersonaTagBadges tags={persona.tags} />
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
        <span className="text-xs text-faint">{agentCountText(persona.agent_count)}</span>
        {isAdmin ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              size="sm"
              onClick={onDuplicate}
              disabled={duplicating}
              aria-label={`Duplicate ${name}`}
            >
              <Copy size={14} /> Duplicate
            </Button>
            {!persona.read_only ? (
              <Button size="sm" onClick={onEdit} aria-label={`Edit ${name}`}>
                <Pencil size={14} /> Edit
              </Button>
            ) : null}
            <Button
              size="sm"
              onClick={onToggle}
              disabled={toggling}
              aria-label={`${persona.enabled ? "Disable" : "Enable"} ${name}`}
            >
              {persona.enabled ? "Disable" : "Enable"}
            </Button>
            {!persona.read_only ? (
              <Button
                size="sm"
                onClick={onDelete}
                disabled={removing}
                aria-label={`Delete ${name}`}
              >
                <Trash2 size={14} /> Delete
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {isAdmin && persona.read_only ? (
        <p className="text-xs text-faint">Built-in cards can’t be edited. Duplicate to make it yours.</p>
      ) : null}
    </li>
  );
}
