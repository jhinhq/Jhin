"use client";

/** The table of contents down the side of the reference: one row per tag group,
 * with the number of operations it holds and the currently-viewed group lit up
 * (the page drives that from a scroll-spy observer). Each row is a real anchor
 * to `#tag-<name>`, so a click jumps, the browser back button returns, and the
 * link is shareable. The same list renders inside the mobile drawer. */

import { focusRing } from "@/components/ui";
import type { TagGroup } from "@/lib/openapi";

export function DocsNav({
  groups,
  activeTag,
  onNavigate,
}: {
  groups: TagGroup[];
  activeTag: string | null;
  onNavigate?: (name: string) => void;
}) {
  if (groups.length === 0) {
    return <p className="px-2 py-1 text-xs text-faint">No sections match.</p>;
  }
  return (
    <ul className="space-y-0.5" data-testid="docs-nav">
      {groups.map((group) => {
        const active = group.name === activeTag;
        return (
          <li key={group.name}>
            <a
              href={`#tag-${group.name}`}
              aria-current={active ? "true" : undefined}
              onClick={() => onNavigate?.(group.name)}
              className={`flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors ${focusRing} ${
                active
                  ? "bg-accent-soft font-semibold text-accent-strong"
                  : "text-dim hover:bg-hover hover:text-ink"
              }`}
            >
              <span className="min-w-0 truncate">{group.name}</span>
              <span
                className={`shrink-0 rounded-full px-1.5 text-[11px] tabular-nums ${
                  active ? "text-accent-strong" : "text-faint"
                }`}
              >
                {group.endpoints.length}
              </span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}
