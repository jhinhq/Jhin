"use client";

/** Browse-library results grid for the Skills page (docs/architecture/skills.md):
 * cards for each skill parsed live out of a known GitHub source, with a
 * one-click Install action. Pure presentational so it is component-testable. */

import { BookOpen, Check, Download } from "lucide-react";
import { Badge, Button, EmptyState } from "@/components/ui";
import type { BrowseSkillEntry } from "@/lib/types";

export function SkillsBrowseGallery({
  entries,
  sourceLabel,
  canInstall,
  installingPath,
  onInstall,
}: {
  entries: BrowseSkillEntry[];
  sourceLabel: string;
  canInstall: boolean;
  installingPath: string | null;
  onInstall: (entry: BrowseSkillEntry) => void;
}) {
  if (entries.length === 0) {
    return (
      <EmptyState
        icon={<BookOpen size={20} aria-hidden />}
        title="No skills found"
        description="No skill in this source matched your search."
      />
    );
  }

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {entries.map((entry) => {
        const installing = installingPath === entry.path;
        return (
          <article
            key={entry.path}
            data-testid={`browse-skill-${entry.name}`}
            className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
          >
            <header className="flex flex-wrap items-center gap-2">
              <code className="truncate font-mono text-sm font-medium text-ink">{entry.name}</code>
              <Badge>{sourceLabel}</Badge>
              {entry.installed ? <Badge tone="ok">Installed</Badge> : null}
            </header>
            <p className="line-clamp-4 text-sm leading-relaxed text-dim">{entry.description}</p>
            <footer className="mt-auto flex items-center justify-between border-t border-line pt-3">
              <span className="truncate text-xs text-faint" title={entry.path}>
                {entry.path}
              </span>
              {canInstall ? (
                <Button
                  size="sm"
                  variant={entry.installed ? "ghost" : "primary"}
                  disabled={entry.installed || installing}
                  onClick={() => onInstall(entry)}
                  aria-label={`Install ${entry.name}`}
                >
                  {entry.installed ? (
                    <>
                      <Check size={13} /> Installed
                    </>
                  ) : (
                    <>
                      <Download size={13} /> {installing ? "Installing…" : "Install"}
                    </>
                  )}
                </Button>
              ) : null}
            </footer>
          </article>
        );
      })}
    </div>
  );
}
