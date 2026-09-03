"use client";

/** The "How you work" block exactly as the agent will read it — rendered by
 * the same rules as `jhin_agents.context.persona_block`, so the preview and
 * the prompt can never disagree. */

import { renderPersonaBlock, type PreviewAudience } from "@/lib/personas";
import type { PersonaFacets } from "@/lib/types";

export function PersonaBlockPreview({
  name,
  displayName,
  facets,
  audience = "both",
  note = true,
}: {
  name: string;
  displayName: string;
  facets: PersonaFacets;
  audience?: PreviewAudience;
  note?: boolean;
}) {
  return (
    <div>
      <pre
        data-testid="persona-block-preview"
        className="whitespace-pre-wrap rounded-xl border border-line bg-raised px-3 py-2 font-mono text-[12px] leading-relaxed text-ink/90"
      >
        {renderPersonaBlock({ name, display_name: displayName, facets }, audience)}
      </pre>
      {note && audience === "both" ? (
        <p className="mt-1.5 text-xs text-faint">
          Only one of “With people” and “With teammates” goes into a run — whichever matches who
          the agent is talking with. A run with nobody on the other side, like a schedule, gets
          neither.
        </p>
      ) : null}
    </div>
  );
}
