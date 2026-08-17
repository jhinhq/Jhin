/** Connection create flow validation and connector-scope helpers (plan 17.9). */

import { describe, expect, it } from "vitest";
import {
  buildCliScope,
  buildConnectorScope,
  capabilityConnectorType,
  CLI_SCOPE_FIELDS,
  findAuthScheme,
  UPCOMING_CONNECTORS,
  validateConnectionForm,
  webhookPayloadUrl,
} from "@/lib/connectors";
import type { ConnectorInfo } from "@/lib/types";

const GITHUB: ConnectorInfo = {
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  description: "Repositories, branches, PRs.",
  auth_schemes: [
    {
      type: "pat",
      label: "Personal access token",
      description: "",
      secret_fields: [
        { name: "token", label: "Token", placeholder: "ghp_…", multiline: false, required: true },
      ],
    },
    {
      type: "github_app",
      label: "GitHub App",
      description: "",
      secret_fields: [
        { name: "app_id", label: "App ID", placeholder: "", multiline: false, required: true },
        {
          name: "private_key",
          label: "Private key",
          placeholder: "",
          multiline: true,
          required: true,
        },
        {
          name: "installation_id",
          label: "Installation ID",
          placeholder: "",
          multiline: false,
          required: true,
        },
      ],
    },
  ],
  config_fields: [],
  webhook_events: ["issues", "pull_request", "push"],
  capabilities: ["github.repository.read"],
  supports_webhooks: true,
  docs_url: "",
};

describe("validateConnectionForm", () => {
  it("accepts a complete PAT form", () => {
    expect(validateConnectionForm(GITHUB, "GitHub main", "pat", { token: "ghp_x" })).toEqual([]);
  });

  it("requires a connector, a name, and a known auth scheme", () => {
    expect(validateConnectionForm(undefined, "x", "pat", {})).toEqual(["Choose a connector."]);
    expect(validateConnectionForm(GITHUB, "  ", "pat", { token: "t" })).toEqual([
      "Name is required.",
    ]);
    expect(validateConnectionForm(GITHUB, "x", "oauth", {})).toEqual([
      "Choose an authentication method.",
    ]);
  });

  it("requires every required credential field of the chosen scheme", () => {
    const errors = validateConnectionForm(GITHUB, "GitHub app", "github_app", {
      app_id: "1",
      private_key: "   ",
    });
    expect(errors).toEqual(["Private key is required.", "Installation ID is required."]);
  });
});

describe("connector scope helpers", () => {
  it("maps capabilities to their owning connector", () => {
    expect(capabilityConnectorType("github.pull_request.create", ["github"])).toBe("github");
    expect(capabilityConnectorType("system.echo", ["github"])).toBeNull();
    expect(capabilityConnectorType("github.repository.read", [])).toBeNull();
  });

  it("builds a scope with only the provided values", () => {
    expect(buildConnectorScope("conn-1", "octo/*", " agent/* ")).toEqual({
      connection_id: "conn-1",
      repository: "octo/*",
      branch: "agent/*",
    });
    expect(buildConnectorScope("", "", "")).toEqual({});
  });

  it("builds cli scopes with only the fields the capability supports", () => {
    expect(
      buildCliScope("cli.command.execute", "conn-1", {
        command: "git *",
        network: "internet",
        repository: "octo/*", // not a command.execute dimension — dropped
      }),
    ).toEqual({ connection_id: "conn-1", command: "git *", network: "internet" });
    expect(
      buildCliScope("cli.repository.checkout", "", { repository: "octo/*", command: "rm *" }),
    ).toEqual({ repository: "octo/*" });
    expect(buildCliScope("cli.file.read", "", { path: "src/*" })).toEqual({ path: "src/*" });
    expect(buildCliScope("cli.file.write", "", {})).toEqual({});
  });

  it("declares scope dimensions for all five cli capabilities", () => {
    expect(Object.keys(CLI_SCOPE_FIELDS).sort()).toEqual([
      "cli.command.execute",
      "cli.file.read",
      "cli.file.write",
      "cli.repository.checkout",
      "cli.test.run",
    ]);
  });
});

describe("gallery data", () => {
  it("lists the roadmap connectors with phases", () => {
    const types = UPCOMING_CONNECTORS.map((c) => c.connector_type);
    expect(types).toEqual(["linear", "vercel", "supabase", "http"]);
    expect(types).not.toContain("cli"); // live since Phase 6
    for (const upcoming of UPCOMING_CONNECTORS) {
      expect(upcoming.phase).toMatch(/^Phase \d+$/);
    }
  });

  it("finds auth schemes and builds webhook URLs", () => {
    expect(findAuthScheme(GITHUB, "github_app")?.label).toBe("GitHub App");
    expect(findAuthScheme(GITHUB, "nope")).toBeUndefined();
    expect(webhookPayloadUrl("/api/v1/webhooks/github/abc", "https://jhin.example/")).toBe(
      "https://jhin.example/api/v1/webhooks/github/abc",
    );
  });
});
