"use client";

/** A persona's facets as a definition list, in prompt order, with the
 * `never` list last. */

import { FACET_SPECS } from "@/lib/personas";
import type { PersonaFacets } from "@/lib/types";

export function PersonaFacetList({ facets }: { facets: PersonaFacets }) {
  return (
    <dl className="grid gap-3 text-sm sm:grid-cols-2">
      {FACET_SPECS.map((spec) => (
        <div key={spec.key}>
          <dt className="text-dim">{spec.label}</dt>
          <dd>{facets[spec.key] || <span className="text-faint">Not set</span>}</dd>
        </div>
      ))}
      <div className="sm:col-span-2">
        <dt className="text-dim">Never</dt>
        <dd>
          {facets.never.length > 0 ? (
            <ul className="list-disc pl-5">
              {facets.never.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <span className="text-faint">Nothing listed</span>
          )}
        </dd>
      </div>
    </dl>
  );
}
