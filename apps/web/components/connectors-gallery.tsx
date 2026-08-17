"use client";

/** Connector gallery (plan 17.9): live connectors are actionable, upcoming
 * ones are greyed out with their arrival phase. Pure presentational so it is
 * component-testable. */

import { GitBranch, Plug } from "lucide-react";
import { Badge, Button } from "@/components/ui";
import { UPCOMING_CONNECTORS } from "@/lib/connectors";
import type { ConnectorInfo } from "@/lib/types";

function ConnectorIcon({ icon }: { icon: string }) {
  if (icon === "github") return <GitBranch size={18} className="text-ink" />;
  return <Plug size={18} className="text-ink" />;
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
          className="flex flex-col gap-3 rounded-xl border border-line bg-surface px-5 py-4"
        >
          <header className="flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg border border-line bg-raised">
              <ConnectorIcon icon={connector.icon} />
            </span>
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold">{connector.display_name}</h3>
              <p className="text-xs text-dim">
                {connector.auth_schemes.map((scheme) => scheme.label).join(" · ")}
              </p>
            </div>
            <Badge tone="ok">live</Badge>
          </header>
          <p className="text-xs leading-relaxed text-dim">{connector.description}</p>
          <footer className="mt-auto flex items-center justify-between border-t border-line pt-3">
            <span className="text-[11px] text-faint">
              {connector.capabilities.length} capabilities
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
      {UPCOMING_CONNECTORS.map((upcoming) => (
        <article
          key={upcoming.connector_type}
          data-testid={`connector-${upcoming.connector_type}`}
          className="flex flex-col gap-3 rounded-xl border border-dashed border-line bg-surface/50 px-5 py-4 opacity-70"
        >
          <header className="flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg border border-line bg-raised">
              <Plug size={18} className="text-faint" />
            </span>
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold text-dim">
                {upcoming.display_name}
              </h3>
            </div>
            <Badge tone="accent">Arrives in {upcoming.phase}</Badge>
          </header>
          <p className="text-xs leading-relaxed text-faint">{upcoming.description}</p>
        </article>
      ))}
    </div>
  );
}
