/** Apps library helpers: filtering, connected state, connect targets. */

import { describe, expect, it } from "vitest";
import {
  ALL_CATEGORIES,
  catalogCategories,
  connectionsForApp,
  connectTarget,
  describeRisk,
  filterCatalog,
  nativeTarget,
} from "@/lib/apps";
import type { CatalogApp, ConnectionInfo, ConnectorInfo } from "@/lib/types";

function entry(overrides: Partial<CatalogApp>): CatalogApp {
  return {
    slug: "x",
    name: "X",
    category: "Developer tools",
    icon: "plug",
    description: "",
    connector_type: null,
    sign_in: "auto",
    mcp_url: null,
    url_unverified: false,
    transport: "unknown",
    auth_hint: "bearer",
    auth_note: "",
    docs_url: "",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
    ...overrides,
  };
}

const GITHUB = entry({ slug: "github", name: "GitHub", connector_type: "github", mcp_url: "https://api.githubcopilot.com/mcp/", description: "Repos and PRs" });
const NOTION = entry({ slug: "notion", name: "Notion", category: "Documents & knowledge", mcp_url: "https://mcp.notion.com/mcp", transport: "streamable_http", auth_hint: "oauth", auth_note: "OAuth only; self-host instead." });
const SLACK = entry({ slug: "slack", name: "Slack", category: "Communication", url_unverified: true, setup_note: "Run a community server." });
const ATLASSIAN = entry({ slug: "atlassian", name: "Atlassian", category: "Project management", mcp_url: "https://mcp.atlassian.com/v1/sse", transport: "sse", auth_hint: "none" });
const FILES = entry({ slug: "filesystem", name: "Filesystem", category: "Storage", url_unverified: true, stdio_only: true, setup_note: "stdio not supported yet." });
/** The case the whole `sign_in` field exists for: a provider that runs its own
 * MCP server *and* has a native Jhin connector. */
const SUPABASE = entry({
  slug: "supabase",
  name: "Supabase",
  connector_type: "supabase",
  sign_in: "remote_mcp",
  mcp_url: "https://mcp.supabase.com/mcp",
  transport: "streamable_http",
  auth_hint: "oauth",
  setup_note: "SQL access is a separate step.",
  connector_config: { project_ref: "abc" },
});
const ENTRIES = [GITHUB, NOTION, SLACK, ATLASSIAN, FILES];

const CONNECTOR = (type: string): ConnectorInfo => ({
  connector_type: type,
  display_name: type,
  icon: type,
  description: "",
  auth_schemes: [],
  config_fields: [],
  webhook_events: [],
  canonical_events: [],
  capabilities: [],
  supports_webhooks: false,
  webhook_secret_mode: "none",
  webhook_signature_algorithm: "",
  webhook_setup_help: "",
  docs_url: "",
});
const CONNECTORS = [CONNECTOR("github"), CONNECTOR("mcp")];

const connection = (type: string, config: Record<string, unknown> = {}): ConnectionInfo => ({
  id: `c-${type}-${JSON.stringify(config)}`,
  connector_type: type,
  name: type,
  auth_type: "bearer",
  status: "active",
  public_id: "p",
  config_json: config,
  created_by_user_id: null,
  created_at: "2026-08-20T00:00:00Z",
  last_verified_at: null,
  last_error: null,
  webhook_secret_configured: false,
});

describe("filterCatalog", () => {
  it("searches name, description, category, and slug case-insensitively", () => {
    expect(filterCatalog(ENTRIES, "repos", ALL_CATEGORIES).map((e) => e.slug)).toEqual(["github"]);
    expect(filterCatalog(ENTRIES, "COMMUNICATION", ALL_CATEGORIES).map((e) => e.slug)).toEqual(["slack"]);
    expect(filterCatalog(ENTRIES, "  ", ALL_CATEGORIES)).toHaveLength(ENTRIES.length);
  });

  it("combines the category filter with the query", () => {
    expect(filterCatalog(ENTRIES, "", "Storage").map((e) => e.slug)).toEqual(["filesystem"]);
    expect(filterCatalog(ENTRIES, "notion", "Storage")).toEqual([]);
    expect(catalogCategories(ENTRIES)).toEqual([
      ALL_CATEGORIES,
      "Developer tools",
      "Documents & knowledge",
      "Communication",
      "Project management",
      "Storage",
    ]);
  });
});

describe("connectionsForApp", () => {
  it("matches native connections by type and MCP connections by server slug", () => {
    const rows = [
      connection("github"),
      connection("mcp", { server_slug: "notion" }),
      connection("mcp", { server_slug: "other" }),
    ];
    expect(connectionsForApp(GITHUB, rows)).toHaveLength(1);
    expect(connectionsForApp(NOTION, rows).map((c) => c.config_json.server_slug)).toEqual(["notion"]);
    expect(connectionsForApp(SLACK, rows)).toEqual([]);
  });
});

describe("connectTarget", () => {
  it("routes to the native connector when Jhin has one", () => {
    const target = connectTarget(GITHUB, CONNECTORS);
    expect(target.kind).toBe("native");
    if (target.kind !== "native") return;
    expect(target.connector.connector_type).toBe("github");
    expect(target.prefill.name).toBe("GitHub");
    expect(target.prefill.config).toEqual({});
  });

  it("pre-fills native connector config from the catalog entry", () => {
    const WEB = entry({
      slug: "web_search_tavily",
      name: "Web search (Tavily)",
      category: "Developer tools",
      connector_type: "web",
      auth_note: "Use a Tavily API key.",
      connector_config: { search_backend: "tavily" },
    });
    const target = connectTarget(WEB, [...CONNECTORS, CONNECTOR("web")]);
    expect(target.kind).toBe("native");
    if (target.kind !== "native") return;
    expect(target.prefill.name).toBe("Web search (Tavily)");
    expect(target.prefill.config).toEqual({ search_backend: "tavily" });
    expect(target.prefill.hint).toContain("Tavily API key");
  });

  it("pre-fills the MCP connector from a verified endpoint", () => {
    const target = connectTarget(NOTION, CONNECTORS);
    expect(target.kind).toBe("mcp");
    if (target.kind !== "mcp") return;
    expect(target.prefill.config).toEqual({
      server_slug: "notion",
      server_url: "https://mcp.notion.com/mcp",
      transport: "auto",
    });
    // An OAuth server is offered the sign-in it actually has. The bearer box
    // it used to get asked for a token the provider never issues.
    expect(target.prefill.authType).toBe("oauth");
    expect(target.prefill.name).toBe("Notion");
    expect(target.prefill.hint).toContain("OAuth only");
  });

  it("leaves the URL empty and asks for it when the endpoint is unverified", () => {
    const target = connectTarget(SLACK, CONNECTORS);
    expect(target.kind).toBe("mcp");
    if (target.kind !== "mcp") return;
    expect(target.prefill.config.server_url).toBe("");
    expect(target.prefill.hint).toContain("Enter the server URL from the provider's docs.");
    expect(target.prefill.hint).toContain("Run a community server.");
  });

  it("uses the SSE transport and no auth when the catalog says so", () => {
    const target = connectTarget(ATLASSIAN, CONNECTORS);
    if (target.kind !== "mcp") throw new Error(target.kind);
    expect(target.prefill.config.transport).toBe("sse");
    expect(target.prefill.authType).toBe("none");
  });

  it("reports stdio-only servers and a missing MCP connector as unsupported", () => {
    expect(connectTarget(FILES, CONNECTORS)).toEqual({ kind: "unsupported", reason: "stdio not supported yet." });
    expect(connectTarget(NOTION, [CONNECTOR("github")]).kind).toBe("unsupported");
  });
});

describe("connectTarget and sign_in", () => {
  const CONNECTORS_WITH_SUPABASE = [...CONNECTORS, CONNECTOR("supabase")];

  it("sends a remote_mcp entry to the provider's own server, native connector or not", () => {
    const target = connectTarget(SUPABASE, CONNECTORS_WITH_SUPABASE);
    expect(target.kind).toBe("mcp");
    if (target.kind !== "mcp") return;
    expect(target.connector.connector_type).toBe("mcp");
    expect(target.prefill.authType).toBe("oauth");
    expect(target.prefill.config).toEqual({
      server_slug: "supabase",
      server_url: "https://mcp.supabase.com/mcp",
      transport: "auto",
    });
  });

  it("leaves an 'auto' entry routing exactly as it always did", () => {
    // The same row without the classification is still the native connector's:
    // the field only ever changes what it was set on.
    const target = connectTarget(entry({ ...SUPABASE, sign_in: "auto" }), CONNECTORS_WITH_SUPABASE);
    expect(target.kind).toBe("native");
    if (target.kind !== "native") return;
    expect(target.connector.connector_type).toBe("supabase");
    expect(connectTarget(GITHUB, CONNECTORS).kind).toBe("native");
  });

  it("still falls through to MCP when the native connector is not installed", () => {
    const target = connectTarget(entry({ ...SUPABASE, sign_in: "auto" }), CONNECTORS);
    expect(target.kind).toBe("mcp");
  });

  it("leaves 'key' and 'none' routing alone — they only change what is said", () => {
    expect(connectTarget(entry({ ...SLACK, sign_in: "key" }), CONNECTORS)).toEqual(
      connectTarget(SLACK, CONNECTORS),
    );
    expect(connectTarget(entry({ ...ATLASSIAN, sign_in: "none" }), CONNECTORS)).toEqual(
      connectTarget(ATLASSIAN, CONNECTORS),
    );
  });
});

describe("nativeTarget", () => {
  it("keeps the native connector reachable for a remote_mcp entry", () => {
    const native = nativeTarget(SUPABASE, [...CONNECTORS, CONNECTOR("supabase")]);
    expect(native).not.toBeNull();
    expect(native?.connector.connector_type).toBe("supabase");
    expect(native?.prefill.config).toEqual({ project_ref: "abc" });
    expect(native?.prefill.hint).toContain("SQL access is a separate step.");
  });

  it("is null when the entry has no native connector, or the server lacks it", () => {
    expect(nativeTarget(NOTION, CONNECTORS)).toBeNull();
    expect(nativeTarget(SUPABASE, CONNECTORS)).toBeNull();
  });
});

describe("plain-language descriptions", () => {
  it("describes risk without jargon", () => {
    expect(describeRisk("read")).toBe("Reads information only");
    expect(describeRisk("destructive")).toContain("needs approval");
  });
});
