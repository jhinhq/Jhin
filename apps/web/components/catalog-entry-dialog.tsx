"use client";

/** The detail sheet for one catalog entry.
 *
 * This is the one screen in the product where text somebody else wrote sits
 * next to a button that creates a connection, so two rules run through it.
 *
 * The dialog dials nothing. It reads the entry from our own API and stops:
 * `mcp_url` is a string shown to a person, never a request this component
 * makes. Nothing is contacted until somebody presses Connect and submits the
 * form, and then it is our API that is contacted, not the indexed server.
 *
 * Only `https://` becomes a link. The catalog model tolerates `http://` in a
 * docs URL, and `javascript:` and `data:` are exactly what a hostile entry
 * would reach for, so the scheme is an allowlist of one and every link
 * carries the full `rel` set — third-party, untrusted, no referrer.
 */

import { useState } from "react";
import { LogoTile } from "@/components/catalog/logo-tile";
import { CreateConnectionDialog, type ConnectionPrefill } from "@/components/connection-create-dialog";
import { Badge, Button, Dialog, ErrorNote, Spinner } from "@/components/ui";
import {
  catalogEntryToApp,
  connectTarget,
  friendlyCatalogName,
  isSafeExternalUrl,
  riskFloorLabel,
  selfHostedTarget,
  trustLabel,
  trustTone,
  type ConnectTarget,
} from "@/lib/apps";
import { normalizeConfigSchema } from "@/lib/config-schema";
import { useCatalogEntry } from "@/lib/hooks";
import type { CatalogEntryDetail, ConnectionCreated, ConnectorInfo } from "@/lib/types";

/** Third-party, untrusted, and not an endorsement — said in the markup. */
const EXTERNAL_REL = "noopener noreferrer nofollow ugc";

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  if (!isSafeExternalUrl(href)) return <span className="text-dim">{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel={EXTERNAL_REL}
      className="text-accent underline underline-offset-2"
    >
      {children}
    </a>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[13px]">
      <span className="text-faint">{label}</span>
      <span className="text-ink">{children}</span>
    </div>
  );
}

function McpFacts({ entry, shownAsSubtitle }: { entry: CatalogEntryDetail; shownAsSubtitle: string | null }) {
  const mcp = entry.mcp;
  if (!mcp) return null;
  return (
    <div className="space-y-1">
      {mcp.tool_count !== null ? <Row label="Tools">{`${mcp.tool_count}`}</Row> : null}
      {mcp.registry_name ? <Row label="Registry name">{mcp.registry_name}</Row> : null}
      {/* The package name already sits under the title when the entry is named
        * after it; repeating it as a fact row said nothing new. */}
      {mcp.npm_package && mcp.npm_package !== shownAsSubtitle ? (
        <Row label="Package">{mcp.npm_package}</Row>
      ) : null}
      {entry.mcp_url ? <Row label="Endpoint">{entry.mcp_url}</Row> : null}
    </div>
  );
}

function SkillFacts({ entry }: { entry: CatalogEntryDetail }) {
  const skill = entry.skill;
  if (!skill) return null;
  return (
    <div className="space-y-1">
      {skill.source_ref ? <Row label="Source">{skill.source_ref}</Row> : null}
      {skill.skill_path ? <Row label="Path">{skill.skill_path}</Row> : null}
      {skill.commit_sha ? <Row label="Commit">{skill.commit_sha}</Row> : null}
      {skill.plugin ? <Row label="Plugin">{skill.plugin}</Row> : null}
      {skill.allowed_tools.length > 0 ? (
        <Row label="Tools it may use">{skill.allowed_tools.join(", ")}</Row>
      ) : null}
      <p className="text-[13px] text-dim">
        A skill is instructions an agent can follow, not a server. There is nothing to connect.
      </p>
    </div>
  );
}

function EntryBody({
  entry,
  target,
  packageName,
  manualConnectable,
  onConnect,
  onManualConnect,
  onClose,
}: {
  entry: CatalogEntryDetail;
  target: ConnectTarget | null;
  /** The raw package name, when the dialog title is a friendlier one. */
  packageName: string | null;
  /** Whether "I have a URL — connect it" has a connector to open. */
  manualConnectable: boolean;
  onConnect: () => void;
  onManualConnect: () => void;
  onClose: () => void;
}) {
  const connectable = entry.connectable && target !== null && target.kind !== "unsupported";
  const blocked = target !== null && target.kind === "unsupported" ? target.reason : null;
  /** Homepage and docs are often the same page; one link is plenty. */
  const homepage = isSafeExternalUrl(entry.homepage) ? entry.homepage : null;
  const docs = isSafeExternalUrl(entry.docs_url) ? entry.docs_url : null;
  const dedupedHomepage = homepage !== null && homepage === docs ? null : homepage;

  return (
    <div className="space-y-5" data-testid="catalog-entry-dialog">
      <div className="flex items-center gap-3">
        <LogoTile name={entry.name} icon={entry.icon} logoUrl={entry.logo_url} size={40} />
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={trustTone(entry.trust_tier)}>{trustLabel(entry.trust_tier)}</Badge>
          <Badge tone="neutral">{entry.category}</Badge>
          {entry.deprecated ? <Badge tone="warn">No longer maintained</Badge> : null}
        </div>
      </div>

      {entry.description ? <p className="text-sm text-ink">{entry.description}</p> : null}

      {entry.tags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {entry.tags.map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-line px-2 py-0.5 text-[12px] text-dim"
            >
              {tag}
            </span>
          ))}
        </div>
      ) : null}

      <div className="space-y-1">
        <Row label="If you connect it">{riskFloorLabel(entry.default_risk)}</Row>
        {entry.license ? <Row label="Licence">{entry.license}</Row> : null}
        {dedupedHomepage ? (
          <Row label="Homepage">
            <ExternalLink href={dedupedHomepage}>{dedupedHomepage}</ExternalLink>
          </Row>
        ) : null}
        {docs ? (
          <Row label="Docs">
            <ExternalLink href={docs}>{docs}</ExternalLink>
          </Row>
        ) : null}
      </div>

      {entry.kind === "skill" ? (
        <SkillFacts entry={entry} />
      ) : (
        <McpFacts entry={entry} shownAsSubtitle={packageName} />
      )}

      {entry.auth_note ? <p className="text-[13px] text-dim">{entry.auth_note}</p> : null}
      {entry.stdio_only ? (
        // The catalog's own note here is developer-speak ("Jhin does not
        // spawn stdio servers…"). Say it plainly instead, and follow with
        // the action a person can actually take.
        <p className="text-[13px] text-dim">
          This app has no hosted address — its server has to be running somewhere first.
          {manualConnectable ? " Already running it? Connect it with its web address below." : ""}
        </p>
      ) : (
        <>
          {entry.setup_note ? <p className="text-[13px] text-dim">{entry.setup_note}</p> : null}
          {blocked && blocked !== entry.setup_note ? (
            <p className="text-[13px] text-dim">{blocked}</p>
          ) : null}
        </>
      )}

      {entry.sources.length > 0 ? (
        <div className="space-y-1">
          <p className="text-[13px] text-faint">Where this listing came from</p>
          {entry.sources.map((source) => (
            <div key={`${source.source_id}:${source.upstream_id}`} className="text-[13px]">
              {isSafeExternalUrl(source.url) ? (
                <ExternalLink href={source.url}>{source.source_id || source.url}</ExternalLink>
              ) : (
                <span className="text-dim">{source.source_id || source.upstream_id}</span>
              )}
            </div>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-start justify-end gap-x-2 gap-y-1">
        <Button type="button" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        {connectable ? (
          <Button type="button" variant="primary" onClick={onConnect}>
            Connect
          </Button>
        ) : null}
        {!connectable && entry.stdio_only && manualConnectable ? (
          <Button type="button" variant="primary" onClick={onManualConnect}>
            I have a URL — connect it
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function CatalogEntryDialog({
  slug,
  workspaceId,
  connectors,
  onClose,
  onCreated,
}: {
  slug: string;
  workspaceId: string;
  connectors: ConnectorInfo[];
  onClose: () => void;
  onCreated: (created: ConnectionCreated) => void;
}) {
  const entry = useCatalogEntry(slug);
  /** Which Connect path is open: the entry's own, or the self-hosted URL one. */
  const [connecting, setConnecting] = useState<"entry" | "manual" | null>(null);

  const detail = entry.data ?? null;
  const app = detail ? catalogEntryToApp(detail) : null;
  // `connectTarget` is the same resolver the curated library uses, reached
  // through a projection rather than a second implementation, so a synced
  // entry and a built-in one connect down exactly one code path.
  const target = app ? connectTarget(app, connectors) : null;
  // A stdio-only entry cannot be dialled, but somebody already hosting it can
  // still point the generic MCP connector at their own URL.
  const manual = app && detail?.stdio_only ? selfHostedTarget(app, connectors) : null;
  // The server's contract when it could build one; anything unusable falls
  // back to the manifest-driven form rather than refusing to open. The manual
  // path skips it — its whole point is a blank URL of the person's own.
  const schema = detail ? normalizeConfigSchema(detail.config_schema) : null;

  // The title a person should read; the raw package name rides underneath.
  const friendly = detail ? friendlyCatalogName(detail.name) : null;

  const activeTarget = connecting === "manual" ? manual : target;
  const prefill: ConnectionPrefill | undefined =
    activeTarget && activeTarget.kind !== "unsupported" ? activeTarget.prefill : undefined;

  return (
    <>
      <Dialog
        title={friendly?.title ?? "App details"}
        description={friendly?.packageName ?? undefined}
        open
        onClose={onClose}
        wide
      >
        {entry.isPending ? <Spinner label="Loading the entry…" /> : null}
        {entry.isError ? <ErrorNote message="This entry could not be loaded." /> : null}
        {detail ? (
          <EntryBody
            entry={detail}
            target={target}
            packageName={friendly?.packageName ?? null}
            manualConnectable={manual !== null}
            onConnect={() => setConnecting("entry")}
            onManualConnect={() => setConnecting("manual")}
            onClose={onClose}
          />
        ) : null}
      </Dialog>

      {connecting !== null && detail && activeTarget && activeTarget.kind !== "unsupported" ? (
        <CreateConnectionDialog
          workspaceId={workspaceId}
          connector={activeTarget.connector}
          prefill={prefill}
          schema={connecting === "entry" ? schema : null}
          onClose={() => setConnecting(null)}
          onCreated={onCreated}
        />
      ) : null}
    </>
  );
}
