"use client";

/** The small labels every persona surface shares: where a card came from,
 * its tags (the `fun` tag first, so the playful cast reads as such), and the
 * "switched off" warning. */

import { Sparkles } from "lucide-react";
import { Badge } from "@/components/ui";
import { FUN_TAG, PERSONA_SOURCE_LABELS } from "@/lib/personas";
import type { PersonaSource } from "@/lib/types";

export function PersonaSourceBadge({ source }: { source: PersonaSource }) {
  return <Badge>{PERSONA_SOURCE_LABELS[source]}</Badge>;
}

export function PersonaTagBadges({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  const ordered = tags.includes(FUN_TAG)
    ? [FUN_TAG, ...tags.filter((tag) => tag !== FUN_TAG)]
    : tags;
  // Spans with list roles rather than <ul>/<li>: this renders inside the
  // picker's <button role="radio"> too, where flow content is invalid HTML.
  return (
    <span role="list" className="flex flex-wrap gap-1.5">
      {ordered.map((tag) => (
        <span role="listitem" key={tag}>
          {tag === FUN_TAG ? (
            <Badge tone="accent">
              <Sparkles size={11} aria-hidden /> fun
            </Badge>
          ) : (
            <Badge tone="neutral">{tag}</Badge>
          )}
        </span>
      ))}
    </span>
  );
}

export function SwitchedOffBadge() {
  return <Badge tone="warn">Switched off</Badge>;
}
