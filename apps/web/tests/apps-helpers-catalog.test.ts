/** Pure helpers behind the synced catalog: the adapter that lets a synced
 * entry reuse the curated Connect path, the trust-tier vocabulary, and the
 * link-safety gate that keeps `http://` and `javascript:` out of the DOM. */

import { describe, expect, it } from "vitest";
import {
  catalogEntryToApp,
  connectTarget,
  FACET_VALUE_LABELS,
  friendlyCatalogName,
  isSafeExternalUrl,
  riskFloorLabel,
  selfHostedTarget,
  TRUST_SHORT,
  trustLabel,
  trustTone,
} from "@/lib/apps";
import type {
  CatalogApp,
  CatalogEntryDetail,
  CatalogTrustTier,
  ConnectorInfo,
} from "@/lib/types";

const MCP_CONNECTOR: ConnectorInfo = {
  connector_type: "mcp",
  display_name: "Any MCP server",
  icon: "mcp",
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
};

const GITHUB_CONNECTOR: ConnectorInfo = {
  ...MCP_CONNECTOR,
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
};

function detail(overrides: Partial<CatalogEntryDetail> = {}): CatalogEntryDetail {
  return {
    slug: "sentry",
    kind: "mcp",
    source: "synced",
    name: "Sentry",
    summary: "Error tracking.",
    category: "Developer tools",
    icon: "bug",
    trust_tier: "registry_verified",
    default_risk: "write",
    popularity: 0.4,
    connector_type: null,
    mcp_url: "https://mcp.sentry.dev/mcp",
    url_unverified: false,
    transport: "streamable_http",
    auth_hint: "bearer",
    stdio_only: false,
    deprecated: false,
    connectable: true,
    docs_url: "https://docs.sentry.io/",
    description: "Error tracking and release health for your services.",
    homepage: "https://sentry.io",
    auth_note: "Create an auth token in Settings.",
    setup_note: "",
    license: "BSL-1.1",
    tags: ["errors", "observability"],
    connector_config: {},
    sources: [],
    config_schema: null,
    mcp: null,
    skill: null,
    ...overrides,
  };
}

/** The same app expressed the curated way, for an apples-to-apples compare. */
function curated(overrides: Partial<CatalogApp> = {}): CatalogApp {
  return {
    slug: "sentry",
    name: "Sentry",
    category: "Developer tools",
    icon: "bug",
    description: "Error tracking and release health for your services.",
    connector_type: null,
    mcp_url: "https://mcp.sentry.dev/mcp",
    url_unverified: false,
    transport: "streamable_http",
    auth_hint: "bearer",
    auth_note: "Create an auth token in Settings.",
    docs_url: "https://docs.sentry.io/",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
    ...overrides,
  };
}

describe("catalogEntryToApp", () => {
  it("resolves to the same connect target as the equivalent curated entry", () => {
    const connectors = [GITHUB_CONNECTOR, MCP_CONNECTOR];
    expect(connectTarget(catalogEntryToApp(detail()), connectors)).toEqual(
      connectTarget(curated(), connectors),
    );
  });

  it("routes a native connector type to its own connector", () => {
    const target = connectTarget(
      catalogEntryToApp(detail({ slug: "github", name: "GitHub", connector_type: "github" })),
      [GITHUB_CONNECTOR, MCP_CONNECTOR],
    );
    expect(target.kind).toBe("native");
    if (target.kind === "native") expect(target.connector.connector_type).toBe("github");
  });

  it("falls back to the summary when the detail carries no description", () => {
    expect(catalogEntryToApp(detail({ description: "" })).description).toBe("Error tracking.");
  });

  it("copies connector_config rather than aliasing it", () => {
    const entry = detail({ connector_config: { search_backend: "brave" } });
    const app = catalogEntryToApp(entry);
    app.connector_config.search_backend = "tampered";
    expect(entry.connector_config.search_backend).toBe("brave");
  });

  it("keeps a stdio-only entry unsupported", () => {
    const target = connectTarget(
      catalogEntryToApp(detail({ stdio_only: true, setup_note: "Run it yourself." })),
      [MCP_CONNECTOR],
    );
    expect(target).toEqual({ kind: "unsupported", reason: "Run it yourself." });
  });
});

describe("friendlyCatalogName", () => {
  it("turns a package name into a title and keeps the package underneath", () => {
    expect(friendlyCatalogName("@notionhq/notion-mcp-server")).toEqual({
      title: "Notion MCP Server",
      packageName: "@notionhq/notion-mcp-server",
    });
    expect(friendlyCatalogName("aws-s3-mcp")).toEqual({
      title: "AWS S3 MCP",
      packageName: "aws-s3-mcp",
    });
  });

  it("leaves a name that already reads as a name untouched", () => {
    expect(friendlyCatalogName("Sentry")).toEqual({ title: "Sentry", packageName: null });
    expect(friendlyCatalogName("Atlassian (Jira & Confluence)")).toEqual({
      title: "Atlassian (Jira & Confluence)",
      packageName: null,
    });
  });
});

describe("selfHostedTarget", () => {
  it("opens the MCP connector with a blank URL of the person's own", () => {
    const target = selfHostedTarget(
      catalogEntryToApp(detail({ slug: "filesystem", name: "filesystem-mcp", stdio_only: true, mcp_url: null })),
      [MCP_CONNECTOR],
    );
    expect(target).toMatchObject({
      kind: "mcp",
      prefill: {
        name: "Filesystem MCP",
        config: { server_slug: "filesystem", server_url: "", transport: "auto" },
      },
    });
  });

  it("returns null when the MCP connector is not installed", () => {
    expect(selfHostedTarget(catalogEntryToApp(detail({ stdio_only: true })), [GITHUB_CONNECTOR])).toBeNull();
  });
});

describe("FACET_VALUE_LABELS", () => {
  it("spells the protocol words the way people do", () => {
    expect(FACET_VALUE_LABELS.sse).toBe("SSE");
    expect(FACET_VALUE_LABELS.oauth).toBe("OAuth");
    expect(FACET_VALUE_LABELS.streamable_http).toBe("Streamable HTTP");
  });
});

describe("trustLabel, trustTone, and TRUST_SHORT", () => {
  const tiers: CatalogTrustTier[] = [
    "curated",
    "registry_verified",
    "smithery_verified",
    "reviewed",
    "indexed",
  ];

  it("names every tier in plain language", () => {
    expect(tiers.map(trustLabel)).toEqual([
      "Reviewed by Jhin",
      "Listed in the official MCP registry",
      "Verified by Smithery — approve each use",
      "From a skill library the Jhin team reviewed — approve each use",
      "Found by crawling — approve each use",
    ]);
  });

  it("colours reassurance, neutrality, and caution apart", () => {
    expect(tiers.map(trustTone)).toEqual(["ok", "ok", "neutral", "ok", "warn"]);
  });

  it("keeps the badge labels short, one per tier", () => {
    expect(tiers.map((tier) => TRUST_SHORT[tier])).toEqual([
      "Curated",
      "Verified",
      "Community",
      "Reviewed",
      "Community",
    ]);
  });
});

describe("riskFloorLabel", () => {
  it("says what the floor means without policy jargon", () => {
    expect(riskFloorLabel("write")).toBe("Can create or change things");
    expect(riskFloorLabel("elevated")).toBe("Needs a person to approve each use");
  });
});

describe("isSafeExternalUrl", () => {
  it("accepts https and nothing else", () => {
    expect(isSafeExternalUrl("https://example.com/docs")).toBe(true);
    expect(isSafeExternalUrl("http://example.com/docs")).toBe(false);
    expect(isSafeExternalUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeExternalUrl("data:text/html,<script>alert(1)</script>")).toBe(false);
    expect(isSafeExternalUrl("ftp://example.com/x")).toBe(false);
    expect(isSafeExternalUrl("//example.com")).toBe(false);
    expect(isSafeExternalUrl("not a url")).toBe(false);
    expect(isSafeExternalUrl("")).toBe(false);
  });

  it("judges the scheme the way the browser will resolve it", () => {
    // The URL parser normalises both of these to the same https origin a
    // browser would follow, so accepting them is the honest answer.
    expect(isSafeExternalUrl("HTTPS://EXAMPLE.COM/x")).toBe(true);
    expect(isSafeExternalUrl("https:example.com")).toBe(true);
    // …while a scheme that only looks like https is still rejected.
    expect(isSafeExternalUrl("httpss://example.com")).toBe(false);
    expect(isSafeExternalUrl(" javascript:alert(1)")).toBe(false);
  });
});
