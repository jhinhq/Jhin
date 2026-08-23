/**
 * Pure helpers for the connectors gallery, connection create flow, and
 * connector-scoped grants (plan 11, 17.9). React-free and unit-tested.
 */

import type { AuthSchemeSpec, ConfigFieldSpec, ConnectorInfo, ToolInfo } from "@/lib/types";

export type ToolScopeValues = Record<string, string>;

export function buildToolScope(tool: ToolInfo, values: ToolScopeValues): Record<string, string> {
  return Object.fromEntries(
    tool.scope_keys.flatMap((key) => {
      const value = (values[key] ?? "").trim();
      return value ? [[key, value]] : [];
    }),
  );
}

export function missingRequiredScopeKeys(tool: ToolInfo, values: ToolScopeValues): string[] {
  return tool.required_grant_scope_keys.filter((key) => !(values[key] ?? "").trim());
}

export function configFieldsForAuth(
  connector: ConnectorInfo,
  authType: string,
): ConfigFieldSpec[] {
  return connector.config_fields.filter(
    (field) => field.auth_types.length === 0 || field.auth_types.includes(authType),
  );
}

export function coerceConnectorConfig(
  fields: ConfigFieldSpec[],
  raw: Record<string, string | boolean>,
): Record<string, string | number | boolean | string[]> {
  const config: Record<string, string | number | boolean | string[]> = {};
  for (const field of fields) {
    const value = raw[field.name] ?? field.default;
    if (value === null || value === undefined || value === "") continue;
    if (field.kind === "integer") {
      config[field.name] = typeof value === "number" ? value : Number(value);
    } else if (field.kind === "boolean") {
      config[field.name] = value === true;
    } else if (field.kind === "string_list") {
      const entries = Array.isArray(value) ? value : String(value).split("\n");
      config[field.name] = [...new Set(entries.map((entry) => entry.trim()).filter(Boolean))];
    } else {
      config[field.name] = String(value).trim();
    }
  }
  return config;
}

export function findAuthScheme(
  connector: ConnectorInfo | undefined,
  authType: string,
): AuthSchemeSpec | undefined {
  return connector?.auth_schemes.find((scheme) => scheme.type === authType);
}

/** Client-side mirror of the API's manifest validation for instant feedback. */
export function validateConnectionForm(
  connector: ConnectorInfo | undefined,
  name: string,
  authType: string,
  credentials: Record<string, string>,
): string[] {
  const errors: string[] = [];
  if (!connector) {
    errors.push("Choose a connector.");
    return errors;
  }
  if (name.trim().length === 0) errors.push("Name is required.");
  if (name.length > 200) errors.push("Name must be at most 200 characters.");
  const scheme = findAuthScheme(connector, authType);
  if (!scheme) {
    errors.push("Choose an authentication method.");
    return errors;
  }
  for (const field of scheme.secret_fields) {
    if (field.required && !(credentials[field.name] ?? "").trim()) {
      errors.push(`${field.label} is required.`);
    }
  }
  return errors;
}

/** Connector type owning a capability like "github.pull_request.create". */
export function capabilityConnectorType(
  capability: string,
  connectorTypes: string[],
): string | null {
  const prefix = capability.split(".", 1)[0];
  return connectorTypes.includes(prefix) ? prefix : null;
}

/** Grant scope_json for a connector capability (plan 12.3): connection plus
 * optional repository/branch glob patterns. Empty inputs are omitted so the
 * grant stays as broad as the admin intended. */
export function buildConnectorScope(
  connectionId: string,
  repository: string,
  branch: string,
): Record<string, string> {
  const scope: Record<string, string> = {};
  if (connectionId) scope.connection_id = connectionId;
  if (repository.trim()) scope.repository = repository.trim();
  if (branch.trim()) scope.branch = branch.trim();
  return scope;
}

/** Scope dimensions each cli.* capability supports (plan 11.6). Only fields
 * a tool actually sends may be constrained — a grant constraining a key the
 * request never carries would fail closed and deny every call. */
export const CLI_SCOPE_FIELDS: Record<string, ReadonlyArray<"command" | "image" | "network" | "repository" | "path">> = {
  "cli.command.execute": ["command", "image", "network"],
  "cli.test.run": ["command", "image"],
  "cli.repository.checkout": ["repository", "image"],
  "cli.file.read": ["path"],
  "cli.file.write": ["path"],
};

/** Grant scope_json for a cli.* capability: connection plus the fnmatch
 * patterns the capability supports. Empty inputs are omitted (unscoped). */
export function buildCliScope(
  capability: string,
  connectionId: string,
  values: { command?: string; image?: string; network?: string; repository?: string; path?: string },
): Record<string, string> {
  const scope: Record<string, string> = {};
  if (connectionId) scope.connection_id = connectionId;
  for (const field of CLI_SCOPE_FIELDS[capability] ?? []) {
    const value = (values[field] ?? "").trim();
    if (value) scope[field] = value;
  }
  return scope;
}

/** Absolute webhook payload URL for display, from the API-relative path. */
export function webhookPayloadUrl(urlPath: string, origin: string): string {
  return `${origin.replace(/\/$/, "")}${urlPath}`;
}

/** Server slug of an MCP tool name like "mcp.fake.echo" (null otherwise). */
export function mcpServerSlug(toolName: string): string | null {
  const parts = toolName.split(".");
  return parts.length === 3 && parts[0] === "mcp" && parts[1] ? parts[1] : null;
}

/** The grant pattern covering every tool on one MCP server. */
export function mcpWildcardCapability(serverSlug: string): string {
  return `mcp.${serverSlug}.*`;
}
