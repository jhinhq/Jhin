/**
 * Pure helpers for the Apps library (docs/architecture/mcp.md): filtering,
 * connected-state detection, and how a catalog entry turns into a
 * pre-filled connection dialog. React-free and unit-tested.
 */

import type {
  CatalogApp,
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
 * connector type, or an MCP connection whose server slug matches. */
export function connectionsForApp(entry: CatalogApp, connections: ConnectionInfo[]): ConnectionInfo[] {
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
