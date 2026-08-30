/** Connection create flow validation and connector-scope helpers (plan 17.9). */

import { describe, expect, it } from "vitest";
import {
  buildToolScope,
  coerceConnectorConfig,
  configFieldsForAuth,
  findAuthScheme,
  missingRequiredScopeKeys,
  validateConnectionForm,
  webhookPayloadUrl,
} from "@/lib/connectors";
import type { ConnectorInfo, ToolInfo } from "@/lib/types";

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
  canonical_events: [],
  capabilities: ["github.repository.read"],
  supports_webhooks: true,
  webhook_secret_mode: "generated",
  webhook_signature_algorithm: "hmac-sha256",
  webhook_setup_help: "Store the generated secret.",
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
  const tool: ToolInfo = {
    name: "vercel.deployment.read",
    description: "Read deployment",
    risk: "read",
    required_capability: "vercel.deployment.read",
    supports_approval: false,
    scope_keys: ["connection_id", "project_id", "deployment_id"],
    required_grant_scope_keys: ["connection_id", "project_id"],
    input_schema: {},
  };

  it("builds only declared per-tool scope and identifies missing required keys", () => {
    expect(buildToolScope(tool, {
      connection_id: " conn-1 ",
      project_id: "prj-1",
      deployment_id: "",
      repository: "must-not-leak",
    })).toEqual({ connection_id: "conn-1", project_id: "prj-1" });
    expect(missingRequiredScopeKeys(tool, { connection_id: "conn-1", project_id: " " })).toEqual([
      "project_id",
    ]);
  });

});

describe("gallery data", () => {
  it("finds auth schemes and builds webhook URLs", () => {
    expect(findAuthScheme(GITHUB, "github_app")?.label).toBe("GitHub App");
    expect(findAuthScheme(GITHUB, "nope")).toBeUndefined();
    expect(webhookPayloadUrl("/api/v1/webhooks/github/abc", "https://jhin.example/")).toBe(
      "https://jhin.example/api/v1/webhooks/github/abc",
    );
  });
});

describe("typed connector config", () => {
  const connector: ConnectorInfo = {
    ...GITHUB,
    connector_type: "supabase",
    config_fields: [
      { name: "project_ref", label: "Project reference", required: true, placeholder: "", help: "", kind: "text", auth_types: ["management_token", "postgres"], default: null, minimum: null, maximum: null },
      { name: "base_url", label: "Base URL", required: false, placeholder: "", help: "", kind: "text", auth_types: ["management_token"], default: "https://api.supabase.com", minimum: null, maximum: null },
      { name: "allowed_schemas", label: "Allowed schemas", required: false, placeholder: "", help: "", kind: "string_list", auth_types: ["postgres"], default: ["public"], minimum: null, maximum: null },
      { name: "allow_writes", label: "Allow writes", required: false, placeholder: "", help: "", kind: "boolean", auth_types: ["postgres"], default: false, minimum: null, maximum: null },
      { name: "max_rows", label: "Max rows", required: false, placeholder: "", help: "", kind: "integer", auth_types: ["postgres"], default: 200, minimum: 1, maximum: 1000 },
    ],
  };

  it("filters fields by auth and excludes values from the previous scheme", () => {
    expect(configFieldsForAuth(connector, "management_token").map((field) => field.name)).toEqual([
      "project_ref",
      "base_url",
    ]);
    expect(configFieldsForAuth(connector, "postgres").map((field) => field.name)).toEqual([
      "project_ref",
      "allowed_schemas",
      "allow_writes",
      "max_rows",
    ]);
  });

  it("coerces integer, boolean, and newline-separated list values", () => {
    const fields = configFieldsForAuth(connector, "postgres");
    expect(coerceConnectorConfig(fields, {
      project_ref: "project-1",
      allowed_schemas: "public\naudit\npublic",
      allow_writes: false,
      max_rows: "250",
    })).toEqual({
      project_ref: "project-1",
      allowed_schemas: ["public", "audit"],
      allow_writes: false,
      max_rows: 250,
    });
  });
});
