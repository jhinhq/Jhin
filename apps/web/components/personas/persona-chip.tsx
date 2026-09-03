"use client";

/** The persona an agent wears, as a chip on the agent profile and the chat
 * header. Links to the card in the library. A persona that is switched off
 * is still shown — dashed, with "· off" — so nobody wonders why the agent
 * stopped sounding like itself. */

import { Drama } from "lucide-react";
import Link from "next/link";
import type { AgentPersonaSummary } from "@/lib/types";

export function PersonaChip({ persona }: { persona: AgentPersonaSummary }) {
  const off = !persona.enabled;
  const name = persona.display_name;
  return (
    <Link
      href={`/personas?persona=${persona.id}`}
      data-testid="persona-chip"
      data-state={off ? "off" : "on"}
      aria-label={off ? `Persona ${name} (switched off)` : `Persona ${name}`}
      title={off ? `${name} is switched off in the library` : `Persona: ${name}`}
      className={`inline-flex max-w-full items-center gap-1 truncate rounded-full border px-2.5 py-0.5 text-xs hover:border-line-strong hover:text-ink ${
        off ? "border-dashed border-line text-faint" : "border-line bg-raised text-dim"
      }`}
    >
      <Drama size={11} aria-hidden />
      <span className="truncate">{name}</span>
      {off ? <span>· off</span> : null}
    </Link>
  );
}
