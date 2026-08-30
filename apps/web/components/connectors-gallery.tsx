"use client";

/** Connector gallery (plan 17.9). Pure presentational so it is
 * component-testable.
 *
 * A connector that declares a sign-in scheme says so on the card, because
 * "Connect" means something different there: no key to find, no key to paste,
 * and a permission that can be revoked at the provider. */

import { Cable, GitBranch, Globe, Plug, Search, Terminal } from "lucide-react";
import { Badge, Button } from "@/components/ui";
import { connectorSignsIn } from "@/lib/oauth";
import type { ConnectorInfo } from "@/lib/types";

function ConnectorIcon({ icon }: { icon: string }) {
  if (icon === "github") return <GitBranch size={18} />;
  if (icon === "terminal") return <Terminal size={18} />;
  if (icon === "mcp") return <Cable size={18} />;
  if (icon === "http") return <Globe size={18} />;
  if (icon === "web") return <Search size={18} />;
  return <Plug size={18} />;
}

export function ConnectorsGallery({
  connectors,
  canManage,
  onConnect,
}: {
  connectors: ConnectorInfo[];
  canManage: boolean;
  onConnect: (connector: ConnectorInfo) => void;
}) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {connectors.map((connector) => (
        <article
          key={connector.connector_type}
          data-testid={`connector-${connector.connector_type}`}
          className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
        >
          <header className="flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent-soft text-accent-strong">
              <ConnectorIcon icon={connector.icon} />
            </span>
            <div className="min-w-0">
              <h3 className="truncate font-display text-sm font-semibold text-ink">{connector.display_name}</h3>
              <p className="text-xs text-dim">
                {connector.auth_schemes.map((scheme) => scheme.label).join(" · ")}
              </p>
            </div>
            {connectorSignsIn(connector) ? <Badge tone="accent">Sign-in</Badge> : null}
            <Badge tone="ok">live</Badge>
          </header>
          <p className="text-sm leading-relaxed text-dim">{connector.description}</p>
          <footer className="mt-auto flex items-center justify-between border-t border-line pt-3">
            <span className="text-xs text-faint">
              {connector.connector_type === "mcp"
                ? "tools discovered after connecting"
                : `${connector.capabilities.length} capabilities`}
              {connector.supports_webhooks ? " · webhooks" : ""}
            </span>
            {canManage ? (
              <Button size="sm" variant="primary" onClick={() => onConnect(connector)}>
                Connect
              </Button>
            ) : null}
          </footer>
        </article>
      ))}
    </div>
  );
}
