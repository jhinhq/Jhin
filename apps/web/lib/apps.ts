/**
 * Pure helpers for the Apps library (docs/architecture/mcp.md): filtering,
 * connected-state detection, and how a catalog entry turns into a
 * pre-filled connection dialog. React-free and unit-tested.
 */

import type { BadgeTone } from "@/components/ui";
import type {
  CatalogApp,
  CatalogEntryDetail,
  CatalogTrustTier,
  ConnectionInfo,
  ConnectionToolInfo,
  ConnectorInfo,
  RiskLevel,
} from "@/lib/types";

export const ALL_CATEGORIES = "All";

/** Categories present in the catalog, in catalog order, with "All" first. */
export function catalogCategories(entries: CatalogApp[]): string[] {
  const seen = new Set<string>();
  for (const entry of entries) seen.add(entry.category);
  return [ALL_CATEGORIES, ...seen];
}

/** Case-insensitive search over name, description, category, and slug plus
 * an optional category filter. */
export function filterCatalog(entries: CatalogApp[], query: string, category: string): CatalogApp[] {
  const needle = query.trim().toLowerCase();
  return entries.filter((entry) => {
    if (category !== ALL_CATEGORIES && entry.category !== category) return false;
    if (!needle) return true;
    return [entry.name, entry.description, entry.category, entry.slug]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
}

/** Connections that belong to a catalog entry: a native connection of its
 * connector type, or an MCP connection whose server slug matches. Takes the
 * two fields it reads rather than a whole `CatalogApp`, so a synced
 * `CatalogEntry` answers the same question without being converted first. */
export function connectionsForApp(
  entry: Pick<CatalogApp, "slug" | "connector_type">,
  connections: ConnectionInfo[],
): ConnectionInfo[] {
  return connections.filter((connection) => {
    if (entry.connector_type && connection.connector_type === entry.connector_type) return true;
    return connection.connector_type === "mcp" && connection.config_json.server_slug === entry.slug;
  });
}

export type ConnectTarget =
  | { kind: "native"; connector: ConnectorInfo; prefill: NativePrefill }
  | { kind: "mcp"; connector: ConnectorInfo; prefill: McpPrefill }
  | { kind: "unsupported"; reason: string };

export interface NativePrefill {
  name: string;
  /** Non-secret config values the catalog entry pins (e.g. search_backend). */
  config: Record<string, string>;
  hint: string | null;
}

export interface McpPrefill {
  name: string;
  authType: "none" | "bearer" | "header";
  config: Record<string, string>;
  /** Shown when the endpoint is unknown or unverified. */
  hint: string | null;
}

/** Words that read as acronyms when a package name is turned into a title. */
const TITLE_ACRONYMS: Record<string, string> = {
  mcp: "MCP", api: "API", ai: "AI", aws: "AWS", gcp: "GCP", sql: "SQL",
  http: "HTTP", https: "HTTPS", url: "URL", cli: "CLI", sdk: "SDK",
  css: "CSS", html: "HTML", json: "JSON", pdf: "PDF", sse: "SSE",
  db: "DB", id: "ID", ui: "UI", llm: "LLM",
};

/**
 * A synced entry often arrives named after its package —
 * `@notionhq/notion-mcp-server` — which is an address, not a name. This
 * derives the title a person should see ("Notion MCP Server") and keeps the
 * raw package name for the subtitle. A name that already reads as a name
 * passes through untouched.
 */
export function friendlyCatalogName(raw: string): { title: string; packageName: string | null } {
  const scoped = /^@[a-z0-9~._-]+\/[a-z0-9~._-]+$/i.test(raw);
  const bare = /^[a-z0-9]+(?:[-_.][a-z0-9]+)+$/.test(raw);
  if (!scoped && !bare) return { title: raw, packageName: null };
  const base = raw.split("/").at(-1) ?? raw;
  const title = base
    .split(/[-_.]+/)
    .filter(Boolean)
    .map((word) => TITLE_ACRONYMS[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
  return { title: title || raw, packageName: raw };
}

function authTypeFor(entry: CatalogApp): McpPrefill["authType"] {
  if (entry.auth_hint === "none") return "none";
  if (entry.auth_hint === "header") return "header";
  // OAuth-only servers still get a bearer form: self-hosted alternatives
  // and provider-issued tokens use it, and the auth note explains the rest.
  return "bearer";
}

/** What pressing Connect on a catalog card should open. Native connectors
 * win; otherwise the generic MCP connector is pre-filled from the entry. */
export function connectTarget(entry: CatalogApp, connectors: ConnectorInfo[]): ConnectTarget {
  if (entry.connector_type) {
    const native = connectors.find((connector) => connector.connector_type === entry.connector_type);
    if (native) {
      const nativeHints: string[] = [];
      if (entry.auth_note) nativeHints.push(entry.auth_note);
      if (entry.setup_note) nativeHints.push(entry.setup_note);
      return {
        kind: "native",
        connector: native,
        prefill: {
          name: entry.name,
          config: { ...(entry.connector_config ?? {}) },
          hint: nativeHints.length > 0 ? nativeHints.join(" ") : null,
        },
      };
    }
  }
  if (entry.stdio_only) {
    return {
      kind: "unsupported",
      reason: entry.setup_note || "This app needs a self-hosted MCP server; stdio servers are not supported yet.",
    };
  }
  const mcp = connectors.find((connector) => connector.connector_type === "mcp");
  if (!mcp) return { kind: "unsupported", reason: "The MCP connector is not installed on this server." };
  const urlKnown = Boolean(entry.mcp_url) && !entry.url_unverified;
  const config: Record<string, string> = {
    server_slug: entry.slug,
    server_url: urlKnown ? (entry.mcp_url as string) : "",
    transport: entry.transport === "sse" ? "sse" : "auto",
  };
  const hints: string[] = [];
  if (!urlKnown) hints.push("Enter the server URL from the provider's docs.");
  if (entry.auth_note) hints.push(entry.auth_note);
  if (entry.setup_note) hints.push(entry.setup_note);
  return {
    kind: "mcp",
    connector: mcp,
    prefill: {
      name: entry.name,
      authType: authTypeFor(entry),
      config,
      hint: hints.length > 0 ? hints.join(" ") : null,
    },
  };
}

/**
 * The connect target for "I have a URL — connect it": a stdio-only entry has
 * no hosted address, but somebody already running the server themselves can
 * still point the generic MCP connector at it. The URL is left blank on
 * purpose — it is theirs to supply.
 */
export function selfHostedTarget(entry: CatalogApp, connectors: ConnectorInfo[]): ConnectTarget | null {
  const mcp = connectors.find((connector) => connector.connector_type === "mcp");
  if (!mcp) return null;
  return {
    kind: "mcp",
    connector: mcp,
    prefill: {
      name: friendlyCatalogName(entry.name).title,
      authType: authTypeFor(entry),
      config: { server_slug: entry.slug, server_url: "", transport: "auto" },
      hint: "Enter the HTTPS address where you run this server.",
    },
  };
}

/** Facet values are lowercase machine words; a few of them are acronyms or
 * phrases that read wrong when the server merely capitalises them ("Sse",
 * "Oauth", "Streamable Http"). The chips prefer these spellings. */
export const FACET_VALUE_LABELS: Record<string, string> = {
  sse: "SSE",
  streamable_http: "Streamable HTTP",
  websocket: "WebSocket",
  oauth: "OAuth",
  bearer: "Bearer token",
  header: "API key header",
  none: "No sign-in",
};

const RISK_COPY: Record<RiskLevel, string> = {
  read: "Reads information only",
  write: "Can create or change things",
  elevated: "Needs a person to approve each use",
  destructive: "Can delete or undo things — needs approval",
};

/** Plain-language risk label for people who do not know the policy model. */
export function describeRisk(risk: RiskLevel): string {
  return RISK_COPY[risk] ?? risk;
}

/** One-line, jargon-free summary of a discovered tool for grant pickers. */
export function describeTool(tool: Pick<ConnectionToolInfo, "name" | "description" | "risk" | "provider_name">): string {
  const label = tool.provider_name ?? tool.name.split(".").at(-1) ?? tool.name;
  const summary = tool.description.replace(/^\[MCP: [^\]]+\]\s*/, "").trim();
  return `${label}: ${summary || "no description from the server"} (${describeRisk(tool.risk).toLowerCase()})`;
}

/* ------------------------------------------------------------------ */
/* The synced catalog (docs/architecture/catalog.md)                    */
/* ------------------------------------------------------------------ */

/**
 * Project a synced catalog entry onto the shape `connectTarget` already
 * understands, so one Connect path serves both halves of the library. The
 * curated entries keep their own `CatalogApp` rows untouched; this only
 * exists so a row that arrived from the sync can reach the same code.
 */
export function catalogEntryToApp(entry: CatalogEntryDetail): CatalogApp {
  return {
    slug: entry.slug,
    name: entry.name,
    category: entry.category,
    icon: entry.icon,
    description: entry.description || entry.summary,
    connector_type: entry.connector_type,
    mcp_url: entry.mcp_url,
    url_unverified: entry.url_unverified,
    transport: entry.transport,
    auth_hint: entry.auth_hint,
    auth_note: entry.auth_note,
    docs_url: entry.docs_url,
    setup_note: entry.setup_note,
    stdio_only: entry.stdio_only,
    connector_config: { ...entry.connector_config },
  };
}

export const TRUST_COPY: Record<CatalogTrustTier, string> = {
  curated: "Reviewed by Jhin",
  registry_verified: "Listed in the official MCP registry",
  smithery_verified: "Verified by Smithery — approve each use",
  reviewed: "From a skill library the Jhin team reviewed — approve each use",
  indexed: "Found by crawling — approve each use",
};

/** Where an entry came from, said plainly. Provenance, not a quality score. */
export function trustLabel(tier: CatalogTrustTier): string {
  return TRUST_COPY[tier] ?? tier;
}

export const TRUST_TONES: Record<CatalogTrustTier, BadgeTone> = {
  curated: "ok",
  registry_verified: "ok",
  smithery_verified: "neutral",
  reviewed: "ok",
  indexed: "warn",
};

/** The one-word badge labels; the full `TRUST_COPY` sentence rides on title. */
export const TRUST_SHORT: Record<CatalogTrustTier, string> = {
  curated: "Curated", registry_verified: "Verified", smithery_verified: "Community",
  reviewed: "Reviewed", indexed: "Community",
};

/** Badge colour for a trust tier: reassuring, plain, or cautionary. */
export function trustTone(tier: CatalogTrustTier): BadgeTone {
  return TRUST_TONES[tier] ?? "neutral";
}

/** How the risk floor an entry's provenance justifies reads to a person. */
export function riskFloorLabel(risk: RiskLevel): string {
  return describeRisk(risk);
}

/**
 * Whether a catalog-supplied URL is safe to render as a link.
 *
 * Only `https://` passes. The catalog model tolerates `http://` in
 * `docs_url`, and `javascript:` / `data:` are exactly what a hostile entry
 * would reach for, so the whitelist is the scheme itself and nothing else.
 */
export function isSafeExternalUrl(url: string): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname !== "";
  } catch {
    return false;
  }
}
