/**
 * Pure helpers for the connectors gallery, connection create flow, and
 * connector-scoped grants (plan 11, 17.9). React-free and unit-tested.
 */

import type { AuthSchemeSpec, ConnectorInfo } from "@/lib/types";

/** Connectors on the roadmap, shown greyed-out in the gallery. */
export interface UpcomingConnector {
  connector_type: string;
  display_name: string;
  description: string;
  phase: string;
}

export const UPCOMING_CONNECTORS: UpcomingConnector[] = [
  {
    connector_type: "linear",
    display_name: "Linear",
    description: "Issues, projects, and triage automation.",
    phase: "Phase 7",
  },
  {
    connector_type: "vercel",
    display_name: "Vercel",
    description: "Deployments, preview URLs, and rollbacks.",
    phase: "Phase 8",
  },
  {
    connector_type: "supabase",
    display_name: "Supabase",
    description: "Database, auth, and storage operations.",
    phase: "Phase 8",
  },
  {
    connector_type: "cli",
    display_name: "CLI",
    description: "Sandboxed shell command execution.",
    phase: "Phase 9",
  },
  {
    connector_type: "http",
    display_name: "HTTP",
    description: "Generic authenticated HTTP requests.",
    phase: "Phase 9",
  },
];

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

/** Absolute webhook payload URL for display, from the API-relative path. */
export function webhookPayloadUrl(urlPath: string, origin: string): string {
  return `${origin.replace(/\/$/, "")}${urlPath}`;
}
