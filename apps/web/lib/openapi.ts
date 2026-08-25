/**
 * Reading the API's own OpenAPI document.
 *
 * The reference at `/api-docs` is not a hand-written page that happens to
 * describe the API; it is a rendering of `GET /api/v1/openapi.json`, which the
 * API generates from its own routes and Pydantic models. Everything in this
 * module is therefore a *reader*: it resolves `$ref`s, flattens schemas into
 * rows a table can show, and builds a curl line — never a second copy of the
 * contract.
 *
 * The endpoint it reads needs a session, which is why the reference lives
 * inside the authenticated shell: in production the anonymous `/openapi.json`
 * and `/docs` are switched off (`EXPOSE_API_DOCS` follows `APP_ENV`), and the
 * surface of a private install is not something to hand to strangers.
 */

import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { api } from "@/lib/api";

/** The origin this person reaches Jhin on, so a curl example can be pasted
 * rather than edited. Read through `useSyncExternalStore` because
 * `window.location` is exactly that — an external store the server render
 * cannot see; the placeholder is what the server (and the first hydration
 * pass) renders. */
const PLACEHOLDER_ORIGIN = "https://your-jhin-host";
const subscribeToNothing = () => () => {};
const readOrigin = () => window.location.origin;
const readPlaceholder = () => PLACEHOLDER_ORIGIN;

export function useApiOrigin(): string {
  return useSyncExternalStore(subscribeToNothing, readOrigin, readPlaceholder);
}

/* -------------------------------------------------------------------------
 * The slice of OpenAPI 3.1 this renderer understands
 * ---------------------------------------------------------------------- */

export interface Schema {
  $ref?: string;
  type?: string | string[];
  format?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  additionalProperties?: Schema | boolean;
  anyOf?: Schema[];
  oneOf?: Schema[];
  allOf?: Schema[];
}

export interface Parameter {
  name: string;
  in: string;
  required?: boolean;
  description?: string;
  schema?: Schema;
}

export interface Body {
  required?: boolean;
  description?: string;
  content?: Record<string, { schema?: Schema }>;
}

export interface Operation {
  operationId?: string;
  summary?: string;
  description?: string;
  tags?: string[];
  deprecated?: boolean;
  parameters?: Parameter[];
  requestBody?: Body;
  responses?: Record<string, Body>;
  security?: Record<string, string[]>[];
  "x-jhin-scope"?: string;
}

export interface SecurityScheme {
  type: string;
  scheme?: string;
  in?: string;
  name?: string;
  bearerFormat?: string;
  description?: string;
}

export interface Spec {
  openapi: string;
  info: {
    title: string;
    version: string;
    summary?: string;
    description?: string;
    license?: { name: string; url?: string };
    contact?: { name?: string; url?: string };
    "x-api-version"?: string;
  };
  servers?: { url: string; description?: string }[];
  tags?: { name: string; description?: string }[];
  paths: Record<string, Record<string, Operation>>;
  components?: {
    schemas?: Record<string, Schema>;
    securitySchemes?: Record<string, SecurityScheme>;
  };
}

const HTTP_METHODS = ["get", "post", "put", "patch", "delete"] as const;
export type HttpMethod = (typeof HTTP_METHODS)[number];

/** The reference reads the session-authenticated copy, which exists in every
 * environment — unlike `/openapi.json`, which production does not serve. */
export function useApiSpec() {
  return useQuery({
    queryKey: ["openapi"],
    queryFn: () => api<Spec>("/api/v1/openapi.json"),
    staleTime: 10 * 60_000,
  });
}

/* -------------------------------------------------------------------------
 * Grouping
 * ---------------------------------------------------------------------- */

export interface Endpoint {
  id: string;
  method: HttpMethod;
  path: string;
  operation: Operation;
  title: string;
  scope: string | null;
  auth: "public" | "session" | "key-or-session";
}

export interface TagGroup {
  name: string;
  description: string;
  endpoints: Endpoint[];
}

/** `list_api_keys_api_v1_...` and `List Api Keys` both come from the handler
 * name, so the title needs a light touch rather than a rewrite. */
export function prettifyTitle(operation: Operation, method: string, path: string): string {
  const summary = operation.summary?.trim();
  if (summary) {
    return summary
      .replace(/\bApi\b/g, "API")
      .replace(/\bUrl\b/g, "URL")
      .replace(/\bId\b/g, "ID")
      .replace(/\bMcp\b/g, "MCP");
  }
  return `${method.toUpperCase()} ${path}`;
}

export function authOf(operation: Operation): Endpoint["auth"] {
  const security = operation.security;
  if (!security || security.length === 0) return "public";
  const schemes = new Set(security.flatMap((entry) => Object.keys(entry)));
  return schemes.has("ApiKeyBearer") ? "key-or-session" : "session";
}

export function endpointId(method: string, path: string): string {
  return `${method}-${path}`.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "");
}

/** Every operation, grouped into the sections the document declares, in the
 * order the document declares them. Tags the spec never described come last. */
export function groupByTag(spec: Spec): TagGroup[] {
  const order = new Map<string, number>();
  const described = new Map<string, string>();
  (spec.tags ?? []).forEach((tag, index) => {
    order.set(tag.name, index);
    described.set(tag.name, tag.description ?? "");
  });

  const groups = new Map<string, Endpoint[]>();
  for (const [path, operations] of Object.entries(spec.paths ?? {})) {
    for (const method of HTTP_METHODS) {
      const operation = operations[method];
      if (!operation) continue;
      const tag = operation.tags?.[0] ?? "other";
      const endpoint: Endpoint = {
        id: endpointId(method, path),
        method,
        path,
        operation,
        title: prettifyTitle(operation, method, path),
        scope: operation["x-jhin-scope"] ?? null,
        auth: authOf(operation),
      };
      const bucket = groups.get(tag);
      if (bucket) bucket.push(endpoint);
      else groups.set(tag, [endpoint]);
    }
  }

  return [...groups.entries()]
    .map(([name, endpoints]) => ({
      name,
      description: described.get(name) ?? "",
      endpoints: endpoints.sort((a, b) => a.path.localeCompare(b.path)),
    }))
    .sort((a, b) => (order.get(a.name) ?? 999) - (order.get(b.name) ?? 999));
}

export function matchesQuery(endpoint: Endpoint, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    endpoint.path.toLowerCase().includes(needle) ||
    endpoint.title.toLowerCase().includes(needle) ||
    endpoint.method.includes(needle) ||
    (endpoint.scope ?? "").toLowerCase().includes(needle)
  );
}

/* -------------------------------------------------------------------------
 * Schemas as rows
 * ---------------------------------------------------------------------- */

export interface FieldRow {
  name: string;
  type: string;
  required: boolean;
  description: string | null;
  enum: string[] | null;
  depth: number;
}

function refName(ref: string): string {
  return ref.slice(ref.lastIndexOf("/") + 1);
}

export function resolve(schema: Schema | undefined, spec: Spec): Schema | undefined {
  if (!schema) return undefined;
  if (!schema.$ref) return schema;
  return spec.components?.schemas?.[refName(schema.$ref)];
}

/** A human name for a schema's type: `string`, `string · uuid`, `Agent[]`,
 * `object`, or the union of an anyOf with the null arm dropped. */
export function typeName(schema: Schema | undefined, spec: Spec, depth = 0): string {
  if (!schema || depth > 6) return "any";
  if (schema.$ref) return refName(schema.$ref);
  const union = schema.anyOf ?? schema.oneOf;
  if (union) {
    const parts = union
      .filter((member) => member.type !== "null")
      .map((member) => typeName(member, spec, depth + 1));
    const unique = [...new Set(parts)];
    const nullable = union.some((member) => member.type === "null");
    const rendered = unique.join(" | ") || "any";
    return nullable ? `${rendered} | null` : rendered;
  }
  if (schema.allOf?.length) return typeName(schema.allOf[0], spec, depth + 1);
  if (schema.type === "array") return `${typeName(schema.items, spec, depth + 1)}[]`;
  const base = Array.isArray(schema.type) ? schema.type.join(" | ") : (schema.type ?? "object");
  return schema.format ? `${base} · ${schema.format}` : base;
}

function enumValues(schema: Schema | undefined, spec: Spec, depth = 0): string[] | null {
  if (!schema || depth > 4) return null;
  const resolved = schema.$ref ? resolve(schema, spec) : schema;
  if (!resolved) return null;
  if (resolved.enum) return resolved.enum.map((value) => JSON.stringify(value));
  const union = resolved.anyOf ?? resolved.oneOf;
  if (union) {
    const collected = union.flatMap((member) => enumValues(member, spec, depth + 1) ?? []);
    return collected.length ? [...new Set(collected)] : null;
  }
  return null;
}

/**
 * Flatten an object schema into table rows, one per field, nested fields
 * indented by `depth`. Bounded on purpose: three levels is as much as a
 * reference table can show usefully, and it stops a self-referential schema
 * (an org tree) from expanding forever.
 */
export function fieldRows(
  schema: Schema | undefined,
  spec: Spec,
  options: { maxDepth?: number } = {},
): FieldRow[] {
  const maxDepth = options.maxDepth ?? 2;
  const rows: FieldRow[] = [];

  const walk = (current: Schema | undefined, prefix: string, depth: number, seen: string[]) => {
    if (!current || depth > maxDepth) return;
    const ref = current.$ref;
    if (ref) {
      if (seen.includes(ref)) return;
      walk(resolve(current, spec), prefix, depth, [...seen, ref]);
      return;
    }
    if (current.type === "array" && current.items) {
      walk(current.items, prefix, depth, seen);
      return;
    }
    if (current.allOf?.length === 1) {
      walk(current.allOf[0], prefix, depth, seen);
      return;
    }
    const properties = current.properties;
    if (!properties) return;
    const required = new Set(current.required ?? []);
    for (const [name, child] of Object.entries(properties)) {
      const full = prefix ? `${prefix}.${name}` : name;
      rows.push({
        name: full,
        type: typeName(child, spec),
        required: required.has(name),
        description: child.description ?? resolve(child, spec)?.description ?? null,
        enum: enumValues(child, spec),
        depth,
      });
      const nested = child.$ref ? resolve(child, spec) : child;
      const target = nested?.type === "array" ? nested.items : nested;
      if (target && (target.properties || target.$ref)) {
        walk(target, full, depth + 1, child.$ref ? [...seen, child.$ref] : seen);
      }
    }
  };

  walk(schema, "", 0, []);
  return rows;
}

export function bodySchema(body: Body | undefined): Schema | undefined {
  const content = body?.content;
  if (!content) return undefined;
  for (const [mediaType, media] of Object.entries(content)) {
    if (mediaType === "application/json" || mediaType.endsWith("+json")) return media.schema;
  }
  return undefined;
}

/** The response an integrator cares about: the documented 2xx, if any. */
export function successResponse(operation: Operation): [string, Body] | null {
  for (const [status, body] of Object.entries(operation.responses ?? {})) {
    if (status.startsWith("2")) return [status, body];
  }
  return null;
}

/* -------------------------------------------------------------------------
 * A copy-pasteable call
 * ---------------------------------------------------------------------- */

export interface CurlOptions {
  origin: string;
  workspaceId?: string;
  token?: string;
}

/** A curl line for one operation, with the caller's own workspace id already
 * substituted so it can be pasted and run rather than read and edited. */
export function curlFor(endpoint: Endpoint, options: CurlOptions): string {
  const token = options.token ?? "jhin_xxxxxxxx_your-key-here";
  const path = endpoint.path.replace(
    "{workspace_id}",
    options.workspaceId ?? "{workspace_id}",
  );
  const lines = [`curl -X ${endpoint.method.toUpperCase()} \\`];
  if (endpoint.auth !== "public") {
    lines.push(`  -H "Authorization: Bearer ${token}" \\`);
  }
  if (endpoint.operation.requestBody) {
    lines.push(`  -H "Content-Type: application/json" \\`);
    lines.push(`  -d '{}' \\`);
  }
  lines.push(`  ${options.origin}${path}`);
  return lines.join("\n");
}

/* -------------------------------------------------------------------------
 * Just enough markdown
 * ---------------------------------------------------------------------- */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; text: string }
  | { kind: "em"; text: string }
  | { kind: "link"; text: string; href: string };

export type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; inline: Inline[] }
  | { kind: "list"; items: Inline[][] }
  | { kind: "code"; text: string };

const INLINE_PATTERN = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|\*[^*\n]+\*)/g;

/** Inline markdown, tokenised. Deliberately small: the API's own prose is the
 * only input, and pulling in a markdown library for it would be a dependency
 * (and a CSP surface) bought for six syntax rules. */
export function parseInline(text: string): Inline[] {
  const tokens: Inline[] = [];
  let cursor = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const index = match.index ?? 0;
    if (index > cursor) tokens.push({ kind: "text", text: text.slice(cursor, index) });
    const piece = match[0];
    if (piece.startsWith("`")) {
      tokens.push({ kind: "code", text: piece.slice(1, -1) });
    } else if (piece.startsWith("**")) {
      tokens.push({ kind: "strong", text: piece.slice(2, -2) });
    } else if (piece.startsWith("[")) {
      const split = piece.indexOf("](");
      tokens.push({
        kind: "link",
        text: piece.slice(1, split),
        href: piece.slice(split + 2, -1),
      });
    } else {
      tokens.push({ kind: "em", text: piece.slice(1, -1) });
    }
    cursor = index + piece.length;
  }
  if (cursor < text.length) tokens.push({ kind: "text", text: text.slice(cursor) });
  return tokens;
}

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let list: string[] = [];
  let fence: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join(" ").trim()) });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list.length) {
      blocks.push({ kind: "list", items: list.map((item) => parseInline(item)) });
      list = [];
    }
  };

  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      if (fence === null) {
        flushParagraph();
        flushList();
        fence = [];
      } else {
        blocks.push({ kind: "code", text: fence.join("\n") });
        fence = null;
      }
      continue;
    }
    if (fence !== null) {
      fence.push(line);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2].trim() });
      continue;
    }
    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (item) {
      flushParagraph();
      list.push(item[1]);
      continue;
    }
    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }
    flushList();
    paragraph.push(line.trim());
  }
  if (fence !== null) blocks.push({ kind: "code", text: fence.join("\n") });
  flushParagraph();
  flushList();
  return blocks;
}
