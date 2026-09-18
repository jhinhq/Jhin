import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../app/globals.css";
import { createRoot } from "react-dom/client";
import { AppLibrary } from "../components/app-library";
import type { CatalogApp, CatalogEntry, ConnectionInfo, ConnectorInfo } from "../lib/types";

const entries: CatalogApp[] = ["supabase", "linear", "vercel"].map((slug) => ({
  slug, name: `${slug[0].toUpperCase()}${slug.slice(1)}`, category: "Developer tools", icon: "plug",
  description: "Provider tools and native connections with browser sign-in available.",
  connector_type: slug, mcp_url: `https://mcp.${slug}.com/mcp`, url_unverified: false,
  transport: "streamable_http", auth_hint: "oauth", auth_note: "", docs_url: "https://example.com/docs",
  setup_note: "", stdio_only: false, connector_config: {}, composio_toolkit: slug,
  sign_in: "remote_mcp",
}));
const connectors: ConnectorInfo[] = [...entries.map((entry) => entry.slug), "mcp"].map((slug) => ({
  connector_type: slug, display_name: slug, icon: "plug", description: "", auth_schemes: [], config_fields: [],
  webhook_events: [], canonical_events: [], capabilities: [], supports_webhooks: false, webhook_secret_mode: "none",
  webhook_signature_algorithm: "", webhook_setup_help: "", docs_url: "",
}));
const connections: ConnectionInfo[] = entries.slice(0, 2).map((entry, index) => ({
  id: entry.slug, name: entry.name, connector_type: entry.slug, auth_type: "oauth",
  status: index === 0 ? "needs_reauth" : "active", public_id: entry.slug, config_json: {},
  created_by_user_id: null, created_at: "2026-09-09T00:00:00Z", last_verified_at: null,
  last_error: null, webhook_secret_configured: false,
}));
const catalogEntries: CatalogEntry[] = entries.map((entry, index) => ({
  ...entry, kind: "mcp", source: "builtin", summary: entry.description,
  trust_tier: "curated", default_risk: "write", popularity: index,
  deprecated: false, connectable: true,
}));
catalogEntries.push({ ...catalogEntries[0], slug: "community-example", name: "Community provider with a long title",
  source: "synced", connector_type: null, trust_tier: "indexed", deprecated: true });
connections.push({ ...connections[0], id: "community", connector_type: "mcp", config_json: { server_slug: "community-example" } });
const params = new URLSearchParams(location.search);
const catalog = params.get("mode") === "catalog";
const noop = () => {};

// Mirrors AppShell's expanded desktop sidebar and the Apps page's default PageBody.
createRoot(document.getElementById("root")!).render(
  <main className="md:ml-[72px] lg:ml-[260px]">
    <div className="mx-auto w-full max-w-6xl px-5 py-6 md:px-8 md:py-8">
      <AppLibrary entries={entries} connectors={connectors} connections={connections}
        canManage={params.get("readonly") !== "1"} onConnect={noop} onOpenConnection={noop} onOpenDetail={noop}
        {...(catalog ? { catalogEntries, catalogTotal: catalogEntries.length, onQueryChange: noop, query: "" } : {})} />
    </div>
  </main>,
);
