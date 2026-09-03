"use client";

/** Pick one persona for an agent (the profile's Persona tab and the wizard).
 * Only switched-on cards are offered: the API refuses a disabled one, and a
 * picker that offers what cannot be picked is a puzzle. */

import { Search, Sparkles } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PersonaTagBadges } from "@/components/personas/persona-badges";
import { focusRing, Input } from "@/components/ui";
import { pickablePersonas } from "@/lib/personas";
import type { Persona } from "@/lib/types";

function optionClass(selected: boolean): string {
  return `block w-full rounded-xl border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${focusRing} ${
    selected ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"
  }`;
}

export function PersonaPicker({
  personas,
  value,
  onChange,
  allowNone = false,
  disabled = false,
}: {
  personas: Persona[];
  value: string | null;
  onChange: (id: string | null) => void;
  allowNone?: boolean;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [funOnly, setFunOnly] = useState(false);
  const anyEnabled = personas.some((persona) => persona.enabled);
  const options = pickablePersonas(personas, query, funOnly);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            aria-hidden
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search personas…"
            className="pl-8"
            aria-label="Search personas"
            disabled={disabled}
          />
        </div>
        <button
          type="button"
          aria-pressed={funOnly}
          disabled={disabled}
          onClick={() => setFunOnly((current) => !current)}
          className={`inline-flex min-h-10 items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:opacity-50 md:min-h-0 ${focusRing} ${
            funOnly
              ? "border-accent bg-accent-soft text-accent-strong"
              : "border-line bg-surface text-dim hover:text-ink"
          }`}
        >
          <Sparkles size={12} aria-hidden /> Fun
        </button>
      </div>
      {!anyEnabled ? (
        <p className="text-sm text-dim">
          No personas are switched on yet.{" "}
          <Link href="/personas" className="text-accent-strong hover:underline">
            Open the Personas page
          </Link>
        </p>
      ) : (
        <div role="radiogroup" aria-label="Persona" className="space-y-2">
          {allowNone ? (
            <button
              type="button"
              role="radio"
              aria-checked={value === null}
              data-testid="persona-option-none"
              disabled={disabled}
              onClick={() => onChange(null)}
              className={optionClass(value === null)}
            >
              <span className="block text-sm font-medium">No persona</span>
              <span className="mt-0.5 block text-xs text-dim">Speaks in its own default way.</span>
            </button>
          ) : null}
          {options.map((persona) => {
            const selected = value === persona.id;
            return (
              <button
                key={persona.id}
                type="button"
                role="radio"
                aria-checked={selected}
                data-testid={`persona-option-${persona.name}`}
                disabled={disabled}
                onClick={() => onChange(persona.id)}
                className={optionClass(selected)}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{persona.display_name}</span>
                  <PersonaTagBadges tags={persona.tags} />
                </span>
                <span className="mt-0.5 block text-xs text-dim">{persona.description}</span>
                <span className="mt-0.5 line-clamp-1 block text-xs italic text-faint">
                  {persona.facets.voice}
                </span>
              </button>
            );
          })}
          {options.length === 0 ? <p className="text-sm text-dim">No personas match.</p> : null}
        </div>
      )}
    </div>
  );
}
