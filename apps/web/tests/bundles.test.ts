import { describe, expect, it } from "vitest";
import {
  agentAccessHref,
  bundleAppliedNotice,
  bundleApplyBody,
  bundleForConnector,
  defaultBundleOptions,
  isConnectorBundle,
  isRepositoryPattern,
  repositoryCoveredBySandbox,
  reviewLines,
  sandboxRepositoryError,
  stepsFor,
} from "@/lib/bundles";
import type { BundleStatusOut, ConnectionInfo } from "@/lib/types";

function connection(overrides: Partial<ConnectionInfo> & Pick<ConnectionInfo, "id" | "connector_type">): ConnectionInfo {
  return {
    name: overrides.id,
    auth_type: "pat",
    status: "active",
    public_id: `pub-${overrides.id}`,
    config_json: {},
    created_by_user_id: null,
    created_at: "2026-09-01T00:00:00Z",
    last_verified_at: null,
    last_error: null,
    webhook_secret_configured: false,
    ...overrides,
  };
}

function bundle(id: string, tools: Record<string, Record<string, string>>): BundleStatusOut {
  return {
    id,
    label: id,
    summary: "",
    description: "",
    tools: Object.entries(tools).map(([name, scope]) => ({ name, capability: name, scope })),
    rules: [],
    not_included: [],
    readiness: { state: "ready", needs: [], missing_tools: [] },
    state: "off",
    granted_capabilities: [],
    missing_capabilities: [],
    problems: [],
  };
}

const CODE_EDITING = bundle("code-editing", {
  "cli.repository.checkout": { repository: "*" },
  "cli.file.read": { path: "*" },
  "github.repository.read": { repository: "*" },
  "github.pull_request.create": { repository: "*", base: "*" },
});
const GITHUB_READ = bundle("github-read", { "github.repository.read": { repository: "*" } });
const WEB_ACCESS = bundle("web-access", { "web.search": {}, "web.fetch": { domain: "*" } });

const github = connection({ id: "gh-1", connector_type: "github", name: "GitHub main" });
const sandbox = connection({
  id: "cli-1",
  connector_type: "cli",
  name: "Sandbox for GitHub main",
  auth_type: "none",
  config_json: { git_connection_id: "gh-1", allowed_repositories: ["octo/*"] },
});
const web = connection({ id: "web-1", connector_type: "web", name: "Web search" });

describe("bundle helpers", () => {
  it("knows which bundles go through a connection", () => {
    expect(isConnectorBundle("code-editing")).toBe(true);
    expect(isConnectorBundle("github-read")).toBe(true);
    expect(isConnectorBundle("web-access")).toBe(true);
    expect(isConnectorBundle("collaboration")).toBe(false);
    expect(bundleForConnector("github")).toBe("github-read");
    expect(bundleForConnector("web")).toBe("web-access");
    expect(bundleForConnector("cli")).toBe("code-editing");
    expect(bundleForConnector("linear")).toBeNull();
  });

  it("builds the deep link the connection drawer sends admins to", () => {
    expect(agentAccessHref("agent-1", "github-read", "gh-1")).toBe(
      "/agents/agent-1?tab=access&bundle=github-read&connection=gh-1",
    );
  });

  it("derives the dialog steps from the bundle's tools", () => {
    expect(stepsFor(CODE_EDITING)).toEqual(["github", "sandbox", "repositories", "review"]);
    expect(stepsFor(GITHUB_READ)).toEqual(["github", "repositories", "review"]);
    expect(stepsFor(WEB_ACCESS)).toEqual(["web", "review"]);
  });

  it("validates repository patterns the way the server does", () => {
    for (const value of ["*", "octo/alpha", "octo/*", "*/*"]) expect(isRepositoryPattern(value)).toBe(true);
    for (const value of ["https://github.com/octo/alpha", "../x", "a/b/c", "", "octo/.."]) {
      expect(isRepositoryPattern(value)).toBe(false);
    }
    expect(sandboxRepositoryError("octo/widgets")).toBeNull();
    expect(sandboxRepositoryError("not a repo")).toBe("Use owner/name, for example octo/widgets");
  });

  it("checks a grant entry against a sandbox allow-list", () => {
    expect(repositoryCoveredBySandbox("octo/alpha", ["*"])).toBe(true);
    expect(repositoryCoveredBySandbox("octo/alpha", ["octo/*"])).toBe(true);
    expect(repositoryCoveredBySandbox("other/alpha", ["octo/*"])).toBe(false);
    expect(repositoryCoveredBySandbox("octo/*", ["octo/*"])).toBe(true);
    expect(repositoryCoveredBySandbox("octo/*", ["octo/alpha"])).toBe(false);
  });

  it("pre-fills the only connection of each type and an existing sandbox", () => {
    const options = defaultBundleOptions(CODE_EDITING, [github, sandbox, web]);
    expect(options.connections).toEqual({ github: "gh-1", cli: "cli-1" });
    expect(options.sandboxMode).toBe("existing");
    expect(options.sandbox.name).toBe("Sandbox for GitHub main");
    expect(options.repositoriesMode).toBe("any");
    expect(options.base).toBe("*");

    const fresh = defaultBundleOptions(CODE_EDITING, [github]);
    expect(fresh.connections).toEqual({ github: "gh-1" });
    expect(fresh.sandboxMode).toBe("create");

    const ambiguous = defaultBundleOptions(
      GITHUB_READ,
      [github, connection({ id: "gh-2", connector_type: "github", name: "Other" })],
    );
    expect(ambiguous.connections).toEqual({});
    const chosen = defaultBundleOptions(
      GITHUB_READ,
      [github, connection({ id: "gh-2", connector_type: "github", name: "Other" })],
      { connectionId: "gh-2" },
    );
    expect(chosen.connections).toEqual({ github: "gh-2" });
  });

  it("posts a sandbox to create only when creating one", () => {
    const creating = defaultBundleOptions(CODE_EDITING, [github]);
    creating.sandbox = { name: "Box", allowedMode: "list", allowed: ["octo/alpha"] };
    expect(bundleApplyBody(CODE_EDITING, creating, true)).toEqual({
      connections: { github: "gh-1" },
      repositories: ["*"],
      base: "*",
      dry_run: true,
      sandbox: { name: "Box", git_connection_id: "gh-1", allowed_repositories: ["octo/alpha"] },
    });
    const existing = defaultBundleOptions(CODE_EDITING, [github, sandbox]);
    existing.repositoriesMode = "list";
    existing.repositories = ["octo/alpha"];
    existing.base = "main";
    expect(bundleApplyBody(CODE_EDITING, existing, false)).toEqual({
      connections: { github: "gh-1", cli: "cli-1" },
      repositories: ["octo/alpha"],
      base: "main",
      dry_run: false,
    });
  });

  it("writes the review lines per bundle", () => {
    const options = defaultBundleOptions(CODE_EDITING, [github, sandbox]);
    expect(reviewLines(CODE_EDITING, options, [github, sandbox])).toEqual([
      "Check out any repository Sandbox for GitHub main allows, browse, search, read and edit files, and run tests inside the sandbox.",
      "Push branches named agent/* — asks for your approval every time, even if this agent is later made Autonomous.",
      "Read repositories, branches, files and pull requests on GitHub; open pull requests (any base branch).",
    ]);
    expect(reviewLines(CODE_EDITING, { ...options, base: "main" }, [github, sandbox])[2]).toContain("base main");
    expect(reviewLines(GITHUB_READ, defaultBundleOptions(GITHUB_READ, [github]), [github])).toEqual([
      "Read repositories, branches, files, issues, pull requests, checks and workflow runs on GitHub. Nothing is written.",
    ]);
    expect(reviewLines(WEB_ACCESS, defaultBundleOptions(WEB_ACCESS, [web]), [web])).toEqual([
      "Search the web and read public pages through Web search.",
    ]);
  });

  it("words the success notice from what was written", () => {
    expect(
      bundleAppliedNotice("Code editing", {
        created_connection: { id: "cli-2" },
        grants_created: new Array(11).fill(0),
        grants_existing: [],
        rules_added: [1],
      }),
    ).toBe("Code editing is on: 1 connection created, 11 grants written, 1 approval rule added.");
    expect(
      bundleAppliedNotice("GitHub (read)", {
        created_connection: null,
        grants_created: [1, 2],
        grants_existing: [1, 2, 3],
        rules_added: [],
      }),
    ).toBe("GitHub (read) is on: 2 grants written, 3 already in place, 0 approval rules added.");
  });
});
