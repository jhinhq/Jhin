/** The client half of the render contract: normalising a schema the server
 * sent, and never trusting it further than the four types it promised.
 *
 * The server already builds this document from installed manifests, so in
 * practice it arrives well-formed. These tests are the belt to that braces:
 * a schema from an older or newer API, or one mangled in transit, must
 * degrade to something renderable rather than throw and take the Connect
 * dialog down with it. `normalizeConfigSchema` never throws — that is the
 * single most important assertion in this file. */

import { describe, expect, it } from "vitest";
import {
  initialValuesFor,
  normalizeConfigSchema,
  splitSubmission,
  validateConfigValues,
  type ConfigSchema,
  type ConfigSchemaField,
  type ConfigValue,
} from "@/lib/config-schema";

function field(overrides: Partial<ConfigSchemaField> = {}): ConfigSchemaField {
  return {
    name: "server_url",
    label: "Server URL",
    type: "string",
    required: false,
    secret: false,
    default: null,
    enum: [],
    max_length: null,
    minimum: null,
    maximum: null,
    placeholder: "",
    help: "",
    multiline: false,
    ...overrides,
  };
}

function schema(fields: ConfigSchemaField[], overrides: Partial<ConfigSchema> = {}): unknown {
  return {
    version: 1,
    connector_type: "mcp",
    fields,
    auth: { type: "bearer", note: "" },
    degraded: [],
    ...overrides,
  };
}

function only(raw: unknown): ConfigSchemaField {
  const normalized = normalizeConfigSchema(raw);
  expect(normalized).not.toBeNull();
  expect(normalized?.fields).toHaveLength(1);
  return (normalized as ConfigSchema).fields[0];
}

describe("normalizeConfigSchema", () => {
  it("keeps a well-formed schema intact", () => {
    const normalized = normalizeConfigSchema(
      schema([
        field({ required: true, default: "https://mcp.example.com/mcp", max_length: 512 }),
        field({ name: "transport", label: "Transport", default: "auto", enum: ["auto", "sse"] }),
      ]),
    );

    expect(normalized).not.toBeNull();
    expect(normalized?.connector_type).toBe("mcp");
    expect(normalized?.auth).toEqual({ type: "bearer", note: "" });
    expect(normalized?.fields.map((item) => item.name)).toEqual(["server_url", "transport"]);
    expect(normalized?.fields[0].default).toBe("https://mcp.example.com/mcp");
  });

  it("never throws, whatever it is handed", () => {
    for (const raw of [null, undefined, 42, "a string", [], {}, { fields: "x" }, { fields: [1, 2] }, NaN]) {
      expect(() => normalizeConfigSchema(raw)).not.toThrow();
      expect(normalizeConfigSchema(raw)).toBeNull();
    }
  });

  it("falls back to null when no field survives, so the dialog uses the manifest form", () => {
    expect(normalizeConfigSchema(schema([]))).toBeNull();
    expect(normalizeConfigSchema(schema([field({ name: "" })]))).toBeNull();
  });

  it("renders an unknown type as a plain text input and records the degradation", () => {
    const normalized = normalizeConfigSchema(
      schema([field({ type: "rich_markdown" as ConfigSchemaField["type"] })]),
    );

    expect(normalized?.fields[0].type).toBe("string");
    expect(normalized?.degraded).toContain("server_url");
  });

  it("honours an enum only between two and twenty distinct usable options", () => {
    expect(only(schema([field({ enum: ["auto", "sse"] })])).enum).toEqual(["auto", "sse"]);
    expect(only(schema([field({ enum: ["auto"] })])).enum).toEqual([]);
    expect(only(schema([field({ enum: [] })])).enum).toEqual([]);
    expect(
      only(schema([field({ enum: Array.from({ length: 30 }, (_, i) => `o${i}`) })])).enum,
    ).toEqual([]);
    expect(only(schema([field({ enum: ["auto", "auto"] })])).enum).toEqual([]);
    expect(only(schema([field({ enum: ["auto", ""] })])).enum).toEqual([]);
    expect(only(schema([field({ enum: ["auto", "x".repeat(201)] })])).enum).toEqual([]);
    expect(only(schema([field({ enum: ["auto", 7] as unknown as string[] })])).enum).toEqual([]);
  });

  it("drops a default whose type does not match the field", () => {
    expect(only(schema([field({ type: "integer", default: "eight" })])).default).toBeNull();
    expect(only(schema([field({ type: "boolean", default: "true" })])).default).toBeNull();
    expect(only(schema([field({ type: "string", default: 8 })])).default).toBeNull();
    expect(only(schema([field({ type: "string_list", default: "a,b" })])).default).toBeNull();
    expect(only(schema([field({ type: "integer", default: 8 })])).default).toBe(8);
    expect(only(schema([field({ type: "boolean", default: false })])).default).toBe(false);
    expect(only(schema([field({ type: "string_list", default: ["a"] })])).default).toEqual(["a"]);
  });

  it("drops a default that is not a member of an honoured enum", () => {
    const honoured = only(schema([field({ enum: ["auto", "sse"], default: "carrier-pigeon" })]));
    expect(honoured.default).toBeNull();

    // With the enum ignored, the same default is just a string again.
    const ignored = only(schema([field({ enum: ["auto"], default: "carrier-pigeon" })]));
    expect(ignored.enum).toEqual([]);
    expect(ignored.default).toBe("carrier-pigeon");
  });

  it("ignores a max_length outside one to two thousand", () => {
    expect(only(schema([field({ max_length: 512 })])).max_length).toBe(512);
    expect(only(schema([field({ max_length: 0 })])).max_length).toBeNull();
    expect(only(schema([field({ max_length: 2001 })])).max_length).toBeNull();
    expect(only(schema([field({ max_length: 1.5 })])).max_length).toBeNull();
    expect(only(schema([field({ max_length: "512" as unknown as number })])).max_length).toBeNull();
  });

  it("ignores bounds on a non-integer field, or bounds that cross", () => {
    const bounded = only(schema([field({ type: "integer", minimum: 1, maximum: 65535 })]));
    expect([bounded.minimum, bounded.maximum]).toEqual([1, 65535]);

    const crossed = only(schema([field({ type: "integer", minimum: 9, maximum: 2 })]));
    expect([crossed.minimum, crossed.maximum]).toEqual([null, null]);

    const half = only(schema([field({ type: "integer", minimum: 5 })]));
    expect([half.minimum, half.maximum]).toEqual([null, null]);

    const texty = only(schema([field({ type: "string", minimum: 1, maximum: 10 })]));
    expect([texty.minimum, texty.maximum]).toEqual([null, null]);
  });

  it("ignores unknown keys on a field and at the top level", () => {
    const normalized = normalizeConfigSchema({
      ...(schema([{ ...field(), injected: "<script>" } as unknown as ConfigSchemaField]) as object),
      surprise: { deeply: "nested" },
    });

    expect(normalized).not.toBeNull();
    expect(normalized?.fields[0]).not.toHaveProperty("injected");
    expect(normalized).not.toHaveProperty("surprise");
  });

  it("renders the first twenty-four fields and no more", () => {
    const many = Array.from({ length: 40 }, (_, index) =>
      field({ name: `field_${index}`, label: `Field ${index}` }),
    );

    expect(normalizeConfigSchema(schema(many))?.fields).toHaveLength(24);
  });

  it("treats an absent or non-array field list as no contract at all", () => {
    expect(normalizeConfigSchema({ version: 1, connector_type: "mcp" })).toBeNull();
    expect(normalizeConfigSchema(schema([]) as object)).toBeNull();
  });

  it("falls back to a bearer note-free auth block when auth is unusable", () => {
    const normalized = normalizeConfigSchema(
      schema([field()], { auth: { type: "kerberos", note: 7 } as unknown as ConfigSchema["auth"] }),
    );

    expect(normalized?.auth.type).toBe("bearer");
    expect(normalized?.auth.note).toBe("");
  });
});

describe("initialValuesFor", () => {
  it("seeds every field from its default, and empty otherwise", () => {
    const normalized = normalizeConfigSchema(
      schema([
        field({ default: "https://mcp.example.com/mcp" }),
        field({ name: "port", label: "Port", type: "integer", default: 8080 }),
        field({ name: "on", label: "On", type: "boolean" }),
        field({ name: "hosts", label: "Hosts", type: "string_list", default: ["a", "b"] }),
        field({ name: "blank", label: "Blank" }),
      ]),
    ) as ConfigSchema;

    expect(initialValuesFor(normalized)).toEqual({
      server_url: "https://mcp.example.com/mcp",
      port: 8080,
      on: false,
      hosts: ["a", "b"],
      blank: "",
    });
  });
});

describe("validateConfigValues", () => {
  const built = (fields: ConfigSchemaField[]) => normalizeConfigSchema(schema(fields)) as ConfigSchema;

  it("says which required field is empty, in a sentence", () => {
    const errors = validateConfigValues(built([field({ required: true })]), { server_url: "" });

    expect(errors).toEqual(["Server URL is required."]);
  });

  it("says what an integer bound actually is", () => {
    const bounded = built([
      field({ name: "port", label: "Port", type: "integer", minimum: 1, maximum: 65535 }),
    ]);

    expect(validateConfigValues(bounded, { port: 70000 })).toEqual([
      "Port must be between 1 and 65535.",
    ]);
    expect(validateConfigValues(bounded, { port: 443 })).toEqual([]);
  });

  it("says when a value is longer than the field allows", () => {
    const capped = built([field({ max_length: 10 })]);
    const errors = validateConfigValues(capped, { server_url: "x".repeat(11) });

    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/Server URL/);
    expect(errors[0].endsWith(".")).toBe(true);
  });

  it("is quiet when everything is fine", () => {
    const fine = built([field({ required: true }), field({ name: "on", label: "On", type: "boolean" })]);

    expect(validateConfigValues(fine, { server_url: "https://mcp.example.com/mcp", on: true })).toEqual(
      [],
    );
  });
});

describe("splitSubmission", () => {
  const built = normalizeConfigSchema(
    schema([
      field({ required: true }),
      field({ name: "api_key", label: "API key", secret: true }),
      field({ name: "notes", label: "Notes" }),
      field({ name: "on", label: "On", type: "boolean" }),
    ]),
  ) as ConfigSchema;

  it("routes secret fields to credentials and everything else to config", () => {
    const values: Record<string, ConfigValue> = {
      server_url: "https://mcp.example.com/mcp",
      api_key: "sk-live-secret",
      notes: "hello",
      on: true,
    };

    const { config, credentials } = splitSubmission(built, values);

    expect(config).toEqual({ server_url: "https://mcp.example.com/mcp", notes: "hello", on: true });
    expect(credentials).toEqual({ api_key: "sk-live-secret" });
    expect(config).not.toHaveProperty("api_key");
  });

  it("drops empty strings from both halves", () => {
    const { config, credentials } = splitSubmission(built, {
      server_url: "https://mcp.example.com/mcp",
      api_key: "",
      notes: "",
      on: false,
    });

    expect(config).toEqual({ server_url: "https://mcp.example.com/mcp", on: false });
    expect(credentials).toEqual({});
  });

  it("ignores values for fields the schema does not declare", () => {
    const { config, credentials } = splitSubmission(built, {
      server_url: "https://mcp.example.com/mcp",
      not_a_field: "smuggled",
    } as Record<string, ConfigValue>);

    expect(config).not.toHaveProperty("not_a_field");
    expect(credentials).not.toHaveProperty("not_a_field");
  });
});
