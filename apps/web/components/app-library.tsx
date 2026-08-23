"use client";

/** The Apps library: search + category filter over the curated catalog,
 * connected state per entry, and a Connect action. Presentational (no data
 * fetching) so it is component-testable. */

import {
  BookOpen,
  Bug,
  Cable,
  Calendar,
  Cloud,
  Cpu,
  CreditCard,
  Database,
  ExternalLink,
  Flame,
  FlaskConical,
  Folder,
  GitBranch,
  Globe,
  HardDrive,
  Kanban,
  LifeBuoy,
  ListTodo,
  Mail,
  MessageCircle,
  MessageSquare,
  Notebook,
  Palette,
  PenTool,
  Phone,
  Plug,
  Search,
  Send,
  Table,
  Terminal,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { Badge, Button, Input, Select } from "@/components/ui";
import { catalogCategories, connectionsForApp, filterCatalog } from "@/lib/apps";
import type { CatalogApp, ConnectionInfo } from "@/lib/types";

const ICONS: Record<string, LucideIcon> = {
  github: GitBranch,
  linear: Kanban,
  vercel: Cloud,
  terminal: Terminal,
  mcp: Cable,
  notebook: Notebook,
  "message-square": MessageSquare,
  "message-circle": MessageCircle,
  kanban: Kanban,
  "credit-card": CreditCard,
  users: Users,
  bug: Bug,
  cloud: Cloud,
  "life-buoy": LifeBuoy,
  zap: Zap,
  "check-square": ListTodo,
  palette: Palette,
  "pen-tool": PenTool,
  folder: Folder,
  calendar: Calendar,
  mail: Mail,
  table: Table,
  database: Database,
  globe: Globe,
  "hard-drive": HardDrive,
  search: Search,
  flame: Flame,
  "book-open": BookOpen,
  send: Send,
  phone: Phone,
  cpu: Cpu,
  flask: FlaskConical,
};

export function AppIcon({ icon, size = 18 }: { icon: string; size?: number }) {
  const Icon = ICONS[icon] ?? Plug;
  return <Icon size={size} aria-hidden />;
}

function howItConnects(entry: CatalogApp): string {
  if (entry.connector_type) return "Built-in connector";
  if (entry.stdio_only) return "Needs a self-hosted server";
  if (entry.mcp_url && !entry.url_unverified) return "Official MCP server";
  return "MCP server (enter its URL)";
}

export function AppLibrary({
  entries,
  connections,
  canManage,
  onConnect,
  onOpenConnection,
}: {
  entries: CatalogApp[];
  connections: ConnectionInfo[];
  canManage: boolean;
  onConnect: (entry: CatalogApp) => void;
  onOpenConnection: (connection: ConnectionInfo) => void;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const categories = catalogCategories(entries);
  const visible = filterCatalog(entries, query, category);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          type="search"
          aria-label="Search apps"
          placeholder="Search apps — Notion, Stripe, Slack…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="sm:max-w-sm"
        />
        <Select
          aria-label="Category"
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          className="sm:w-56"
        >
          {categories.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </Select>
        <span className="text-xs text-faint sm:ml-auto" data-testid="app-count">
          {visible.length} of {entries.length} apps
        </span>
      </div>
      {visible.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-line-strong px-4 py-8 text-center text-sm text-dim">
          No apps match. Any app with an MCP server can still be added with “Any MCP server” under Advanced → Connectors.
        </p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((entry) => {
            const connected = connectionsForApp(entry, connections);
            const connectable = Boolean(entry.connector_type) || !entry.stdio_only;
            return (
              <li
                key={entry.slug}
                data-testid={`app-${entry.slug}`}
                className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
              >
                <header className="flex items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
                    <AppIcon icon={entry.icon} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate font-display text-sm font-semibold text-ink">{entry.name}</h3>
                    <p className="truncate text-xs text-dim">{entry.category} · {howItConnects(entry)}</p>
                  </div>
                  {connected.length > 0 ? (
                    <Badge tone="ok">Connected</Badge>
                  ) : entry.stdio_only ? (
                    <Badge tone="neutral">Self-hosted</Badge>
                  ) : null}
                </header>
                <p className="text-sm leading-relaxed text-dim">{entry.description}</p>
                {entry.stdio_only && entry.setup_note ? (
                  <p className="text-xs text-faint">{entry.setup_note}</p>
                ) : null}
                <footer className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-3">
                  {entry.docs_url ? (
                    <a
                      href={entry.docs_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-accent-strong hover:underline"
                    >
                      <ExternalLink size={12} aria-hidden /> Docs
                    </a>
                  ) : (
                    <span />
                  )}
                  <div className="flex items-center gap-2">
                    {connected.length > 0 ? (
                      <Button size="sm" onClick={() => onOpenConnection(connected[0])}>
                        Manage
                      </Button>
                    ) : null}
                    {canManage && connectable ? (
                      <Button size="sm" variant="primary" onClick={() => onConnect(entry)}>
                        {connected.length > 0 ? "Connect another" : "Connect"}
                      </Button>
                    ) : null}
                  </div>
                </footer>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
