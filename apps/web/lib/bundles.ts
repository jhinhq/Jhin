/**
 * Pure helpers behind the capability bundle setup dialog (Tools & Access)
 * and the "Give to an agent…" action on a connection. React-free; the
 * server (`jhin_policy.bundles`) is the authority on what gets written, and
 * these only shape the questions the dialog asks and the words it uses.
 */

import type { BundleApply, BundleStatusOut, ConnectionInfo } from "@/lib/types";

/** Bundles whose tools go through a workspace connection. Everything else
 * (organization, skills) is written by the client loop the tab always had. */
const CONNECTOR_BUNDLE_IDS = new Set(["code-editing", "github-read", "web-access"]);

/** Connector types that are built in, never a connection. */
const BUILTIN_PREFIXES = new Set(["organization", "skills", "memory"]);

const BUNDLE_FOR_CONNECTOR: Record<string, string> = {
  github: "github-read",
  web: "web-access",
  cli: "code-editing",
};

const CONNECTOR_LABELS: Record<string, string> = {
  github: "GitHub",
  web: "Web",
  cli: "CLI Sandbox",
  linear: "Linear",
  vercel: "Vercel",
  supabase: "Supabase",
  mcp: "MCP",
};

export function isConnectorBundle(bundleId: string): boolean {
  return CONNECTOR_BUNDLE_IDS.has(bundleId);
}

/** The bundle that gives an agent this kind of app, or null when there is
 * none yet (grant tools by hand under the agent's Tools & Access). */
export function bundleForConnector(connectorType: string): string | null {
  return BUNDLE_FOR_CONNECTOR[connectorType] ?? null;
}

export function connectorLabel(connectorType: string): string {
  return (
    CONNECTOR_LABELS[connectorType] ??
    connectorType.charAt(0).toUpperCase() + connectorType.slice(1)
  );
}

/** Where "Give to an agent…" sends an admin: the agent's Tools & Access tab
 * with the setup dialog open on this connection. */
export function agentAccessHref(agentId: string, bundleId: string, connectionId: string): string {
  const params = new URLSearchParams({ tab: "access", bundle: bundleId, connection: connectionId });
  return `/agents/${agentId}?${params.toString()}`;
}

/** Connector types a bundle's tools go through, GitHub first (a sandbox is
 * only ever chosen relative to the GitHub connection it borrows from). */
export function connectorTypesOf(bundle: Pick<BundleStatusOut, "tools">): string[] {
  const types: string[] = [];
  for (const tool of bundle.tools) {
    const prefix = tool.name.split(".", 1)[0];
    if (BUILTIN_PREFIXES.has(prefix) || types.includes(prefix)) continue;
    types.push(prefix);
  }
  return types.sort((a, b) => Number(a !== "github") - Number(b !== "github"));
}

/**
 * The dialog's steps. Connector types are steps of their own ("github",
 * "web"); a `cli` tool means the Sandbox step instead; a tool with a
 * `repository` scope means the Repositories step; Review always closes.
 * Steps whose answer is unambiguous are shown pre-filled, never skipped.
 */
export function stepsFor(bundle: Pick<BundleStatusOut, "id" | "tools">): string[] {
  const steps: string[] = [];
  let sandbox = false;
  for (const type of connectorTypesOf(bundle)) {
    if (type === "cli") sandbox = true;
    else steps.push(type);
  }
  if (sandbox) steps.push("sandbox");
  if (bundle.tools.some((tool) => "repository" in tool.scope)) steps.push("repositories");
  steps.push("review");
  return steps;
}

const REPOSITORY_SEGMENT = /^[A-Za-z0-9_.*-]+$/;

/** Mirror of `jhin_policy.is_repository_pattern`: `*`, `owner/name`, `owner/*`. */
export function isRepositoryPattern(value: string): boolean {
  if (value === "*") return true;
  const segments = value.split("/");
  return (
    segments.length === 2 &&
    segments.every((segment) => segment !== "." && segment !== ".." && REPOSITORY_SEGMENT.test(segment))
  );
}

/** Validation for one allow-list line on the Sandbox step. */
export function sandboxRepositoryError(entry: string): string | null {
  return isRepositoryPattern(entry.trim()) ? null : "Use owner/name, for example octo/widgets";
}

/** Validation for a hand-written `repository` scope in the advanced form. */
export function repositoryScopeError(value: string): string | null {
  return isRepositoryPattern(value.trim()) ? null : "Use owner/name, owner/*, or * for every repository.";
}

function segmentMatches(pattern: string, value: string): boolean {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`).test(value);
}

/** Mirror of `jhin_policy.repository_covered_by_allow_list`. */
export function repositoryCoveredBySandbox(entry: string, allowList: string[]): boolean {
  if (allowList.includes("*")) return true;
  if (entry.includes("*")) return allowList.includes(entry);
  const parts = entry.split("/");
  return allowList.some((pattern) => {
    const patternParts = pattern.split("/");
    return (
      patternParts.length === parts.length &&
      patternParts.every((segment, index) => segmentMatches(segment, parts[index]))
    );
  });
}

export interface BundleOptions {
  /** connector type -> connection id (only the types the bundle needs). */
  connections: Record<string, string>;
  sandboxMode: "create" | "existing";
  sandbox: { name: string; allowedMode: "any" | "list"; allowed: string[] };
  repositoriesMode: "any" | "list";
  repositories: string[];
  base: string;
}

export function activeConnectionsOf(connections: ConnectionInfo[], type: string): ConnectionInfo[] {
  return connections.filter((c) => c.connector_type === type && c.status === "active");
}

export function gitConnectionIdOf(connection: ConnectionInfo): string {
  const value = connection.config_json.git_connection_id;
  return typeof value === "string" ? value : "";
}

export function allowedRepositoriesOf(connection: ConnectionInfo | undefined): string[] {
  const raw = connection?.config_json.allowed_repositories;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
}

/** Sandboxes that borrow this GitHub connection's credential — the only
 * ones the Code editing bundle may point at. */
export function sandboxesFor(connections: ConnectionInfo[], githubId: string): ConnectionInfo[] {
  return activeConnectionsOf(connections, "cli").filter((c) => gitConnectionIdOf(c) === githubId);
}

/** Pre-filled answers: the only active connection of each type, an existing
 * sandbox when one uses that GitHub connection, every repository, base `*`. */
export function defaultBundleOptions(
  bundle: Pick<BundleStatusOut, "id" | "tools">,
  connections: ConnectionInfo[],
  initial: { connectionId?: string } = {},
): BundleOptions {
  const chosen: Record<string, string> = {};
  for (const type of connectorTypesOf(bundle)) {
    if (type === "cli") continue;
    const candidates = activeConnectionsOf(connections, type);
    const preferred = candidates.find((c) => c.id === initial.connectionId);
    if (preferred) chosen[type] = preferred.id;
    else if (candidates.length === 1) chosen[type] = candidates[0].id;
  }
  const github = connections.find((c) => c.id === chosen.github);
  const sandboxes = github ? sandboxesFor(connections, github.id) : [];
  const preferredSandbox =
    sandboxes.find((c) => c.id === initial.connectionId) ?? (sandboxes.length === 1 ? sandboxes[0] : undefined);
  if (preferredSandbox) chosen.cli = preferredSandbox.id;
  return {
    connections: chosen,
    sandboxMode: preferredSandbox ? "existing" : "create",
    sandbox: { name: `Sandbox for ${github?.name ?? "GitHub"}`, allowedMode: "any", allowed: [] },
    repositoriesMode: "any",
    repositories: [],
    base: "*",
  };
}

/** The body the dialog posts, for both the dry run and the real thing. */
export function bundleApplyBody(
  bundle: Pick<BundleStatusOut, "id">,
  options: BundleOptions,
  dryRun: boolean,
): BundleApply {
  const creating = bundle.id === "code-editing" && options.sandboxMode === "create";
  const connections: Record<string, string> = {};
  for (const [type, id] of Object.entries(options.connections)) {
    if (!id) continue;
    if (type === "cli" && creating) continue;
    connections[type] = id;
  }
  const body: BundleApply = {
    connections,
    repositories: options.repositoriesMode === "any" ? ["*"] : options.repositories,
    base: options.base.trim() || "*",
    dry_run: dryRun,
  };
  if (creating && options.connections.github) {
    body.sandbox = {
      name: options.sandbox.name.trim(),
      git_connection_id: options.connections.github,
      allowed_repositories: options.sandbox.allowedMode === "any" ? ["*"] : options.sandbox.allowed,
    };
  }
  return body;
}

/** What the agent will be able to do, in plain language, for the Review
 * step and the CLI's output. */
export function reviewLines(
  bundle: Pick<BundleStatusOut, "id">,
  options: BundleOptions,
  connections: ConnectionInfo[],
): string[] {
  const byId = new Map(connections.map((c) => [c.id, c]));
  if (bundle.id === "code-editing") {
    const sandbox =
      options.sandboxMode === "create"
        ? options.sandbox.name.trim() || "the sandbox"
        : byId.get(options.connections.cli ?? "")?.name ?? "the sandbox";
    const base = options.base.trim() || "*";
    return [
      `Check out any repository ${sandbox} allows, browse, search, read and edit files, and run tests inside the sandbox.`,
      "Push branches named agent/* — asks for your approval every time, even if this agent is later made Autonomous.",
      `Read repositories, branches, files and pull requests on GitHub; open pull requests (${base === "*" ? "any base branch" : `base ${base}`}).`,
    ];
  }
  if (bundle.id === "github-read") {
    return [
      "Read repositories, branches, files, issues, pull requests, checks and workflow runs on GitHub. Nothing is written.",
    ];
  }
  if (bundle.id === "web-access") {
    const connection = byId.get(options.connections.web ?? "")?.name ?? "the Web connection";
    return [`Search the web and read public pages through ${connection}.`];
  }
  return [];
}

/** The line the success notice shows once a bundle is on. */
export function bundleAppliedNotice(
  label: string,
  result: { created_connection: unknown; grants_created: unknown[]; grants_existing: unknown[]; rules_added: unknown[] },
): string {
  const rules = result.rules_added.length;
  return `${label} is on: ${result.created_connection ? "1 connection created, " : ""}${result.grants_created.length} grants written${result.grants_existing.length ? `, ${result.grants_existing.length} already in place` : ""}, ${rules} approval rule${rules === 1 ? "" : "s"} added.`;
}
