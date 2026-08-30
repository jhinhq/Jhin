/**
 * The client half of the Connect form's render contract.
 *
 * The API builds this document from *installed connector manifests* — the
 * catalog only ever supplies values for field names a manifest already
 * declared, never field definitions — so in practice it arrives well-formed.
 * Everything here is the belt to that braces: a schema from an older or newer
 * API, or one mangled in transit, must degrade to something renderable rather
 * than throw and take the dialog down with it.
 *
 * `normalizeConfigSchema` therefore never throws and never returns a partially
 * trusted document. It returns a schema whose every field is one of the four
 * types the form can draw, or `null`, which means "use the manifest-driven
 * form instead" — the path that existed before the catalog and still works.
 *
 * React-free and unit-tested.
 */

export type ConfigFieldType = "string" | "integer" | "boolean" | "string_list";

/** Anything a rendered control can hold. */
export type ConfigValue = string | number | boolean | string[];

export interface ConfigSchemaField {
  name: string;
  label: string;
  type: ConfigFieldType;
  required: boolean;
  secret: boolean;
  default: ConfigValue | null;
  /** Honoured choices, or `[]` when the server's list was not a usable one. */
  enum: string[];
  max_length: number | null;
  minimum: number | null;
  maximum: number | null;
  placeholder: string;
  help: string;
  multiline: boolean;
}

export interface ConfigSchemaAuth {
  type: "none" | "bearer" | "header" | "oauth";
  note: string;
}

export interface ConfigSchema {
  version: number;
  connector_type: string;
  fields: ConfigSchemaField[];
  auth: ConfigSchemaAuth;
  /** Field names the server (or this normaliser) had to simplify. */
  degraded: string[];
}

const FIELD_TYPES: ConfigFieldType[] = ["string", "integer", "boolean", "string_list"];
const AUTH_TYPES: ConfigSchemaAuth["type"][] = ["none", "bearer", "header", "oauth"];

/** A form nobody can read is not a safer form. Past this many fields the
 * server has almost certainly sent something unintended. */
const MAX_FIELDS = 24;
/** Below two options a select is a decoration; above twenty it is a search
 * problem, and a text input is the honest control for one. */
const MIN_ENUM_OPTIONS = 2;
const MAX_ENUM_OPTIONS = 20;
const MAX_ENUM_OPTION_CHARS = 200;
const MAX_LENGTH_CEILING = 2000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asBoolean(value: unknown): boolean {
  return value === true;
}

function asInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

/**
 * The options a select may offer, or `[]`.
 *
 * All or nothing on purpose: a list with one unusable member is a list this
 * build does not understand, and quietly rendering the rest would offer a
 * choice the server never described.
 */
function normalizeEnum(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const usable = raw.filter(
    (option): option is string =>
      typeof option === "string" && option.length > 0 && option.length <= MAX_ENUM_OPTION_CHARS,
  );
  if (usable.length !== raw.length) return [];
  if (usable.length < MIN_ENUM_OPTIONS || usable.length > MAX_ENUM_OPTIONS) return [];
  if (new Set(usable).size !== usable.length) return [];
  return usable;
}

/** A default only survives if it is the field's own type — and, when the
 * field offers choices, one of them. */
function normalizeDefault(
  raw: unknown,
  type: ConfigFieldType,
  options: string[],
): ConfigValue | null {
  if (raw === null || raw === undefined) return null;
  if (type === "string") {
    if (typeof raw !== "string") return null;
    if (options.length > 0 && !options.includes(raw)) return null;
    return raw;
  }
  if (type === "integer") return asInteger(raw);
  if (type === "boolean") return typeof raw === "boolean" ? raw : null;
  if (!Array.isArray(raw) || raw.some((item) => typeof item !== "string")) return null;
  return raw as string[];
}

function normalizeMaxLength(raw: unknown): number | null {
  const value = asInteger(raw);
  if (value === null || value < 1 || value > MAX_LENGTH_CEILING) return null;
  return value;
}

/** Bounds are a pair or they are nothing: half a range cannot be rendered as
 * one, and a crossed range cannot be satisfied at all. */
function normalizeBounds(
  rawMin: unknown,
  rawMax: unknown,
  type: ConfigFieldType,
): [number | null, number | null] {
  if (type !== "integer") return [null, null];
  const minimum = asInteger(rawMin);
  const maximum = asInteger(rawMax);
  if (minimum === null || maximum === null || minimum > maximum) return [null, null];
  return [minimum, maximum];
}

function normalizeField(raw: unknown, degraded: string[]): ConfigSchemaField | null {
  if (!isRecord(raw)) return null;
  const name = asString(raw.name);
  if (!name) return null;

  const declared = raw.type;
  const known = FIELD_TYPES.find((candidate) => candidate === declared);
  // A renderer that meets a type it does not know falls back to a text input
  // rather than refusing the form — and says so, so the degradation is
  // visible rather than silent.
  const type: ConfigFieldType = known ?? "string";
  if (!known) degraded.push(name);

  const options = normalizeEnum(raw.enum);
  const [minimum, maximum] = normalizeBounds(raw.minimum, raw.maximum, type);
  return {
    name,
    label: asString(raw.label) || name,
    type,
    required: asBoolean(raw.required),
    secret: asBoolean(raw.secret),
    default: normalizeDefault(raw.default, type, options),
    enum: options,
    max_length: normalizeMaxLength(raw.max_length),
    minimum,
    maximum,
    placeholder: asString(raw.placeholder),
    help: asString(raw.help),
    multiline: asBoolean(raw.multiline),
  };
}

function normalizeAuth(raw: unknown): ConfigSchemaAuth {
  const fallback: ConfigSchemaAuth = { type: "bearer", note: "" };
  if (!isRecord(raw)) return fallback;
  const type = AUTH_TYPES.find((candidate) => candidate === raw.type);
  if (!type || typeof raw.note !== "string") return fallback;
  return { type, note: raw.note };
}

/**
 * A schema safe to render, or `null` to fall back to the manifest form.
 *
 * Never throws, whatever it is handed. That is the single most important
 * property in this module: it runs on a document from the network, inside a
 * dialog somebody opened on purpose.
 */
export function normalizeConfigSchema(raw: unknown): ConfigSchema | null {
  if (!isRecord(raw)) return null;
  if (!Array.isArray(raw.fields)) return null;

  const degraded: string[] = Array.isArray(raw.degraded)
    ? raw.degraded.filter((item): item is string => typeof item === "string")
    : [];
  const fields: ConfigSchemaField[] = [];
  for (const candidate of raw.fields) {
    if (fields.length >= MAX_FIELDS) break;
    const field = normalizeField(candidate, degraded);
    if (field) fields.push(field);
  }
  if (fields.length === 0) return null;

  return {
    version: asInteger(raw.version) ?? 1,
    connector_type: asString(raw.connector_type),
    fields,
    auth: normalizeAuth(raw.auth),
    degraded,
  };
}

function emptyValueFor(type: ConfigFieldType): ConfigValue {
  if (type === "boolean") return false;
  if (type === "string_list") return [];
  return "";
}

/** The form's starting state: each field's default, or nothing typed yet. */
export function initialValuesFor(schema: ConfigSchema): Record<string, ConfigValue> {
  const values: Record<string, ConfigValue> = {};
  for (const field of schema.fields) {
    values[field.name] = field.default ?? emptyValueFor(field.type);
  }
  return values;
}

function isEmpty(field: ConfigSchemaField, value: ConfigValue | undefined): boolean {
  if (value === undefined) return true;
  if (field.type === "boolean") return false;
  if (Array.isArray(value)) return value.length === 0;
  return String(value).trim() === "";
}

/**
 * What is wrong with the form, as sentences a person can act on.
 *
 * Each message names the field by its label and says what the rule actually
 * is — "between 1 and 65535", not "invalid" — because the person filling this
 * in has never seen the manifest that produced it.
 */
export function validateConfigValues(
  schema: ConfigSchema,
  values: Record<string, ConfigValue>,
): string[] {
  const errors: string[] = [];
  for (const field of schema.fields) {
    const value = values[field.name];
    if (field.required && isEmpty(field, value)) {
      errors.push(`${field.label} is required.`);
      continue;
    }
    if (isEmpty(field, value)) continue;

    if (field.type === "integer" && field.minimum !== null && field.maximum !== null) {
      const numeric = typeof value === "number" ? value : Number(value);
      if (!Number.isFinite(numeric) || numeric < field.minimum || numeric > field.maximum) {
        errors.push(`${field.label} must be between ${field.minimum} and ${field.maximum}.`);
        continue;
      }
    }
    if (field.max_length !== null && !Array.isArray(value) && typeof value !== "boolean") {
      if (String(value).length > field.max_length) {
        errors.push(`${field.label} must be ${field.max_length} characters or fewer.`);
      }
    }
  }
  return errors;
}

/**
 * Split what was typed into the two halves the API takes.
 *
 * Secrets go to `credentials`, which the API stores encrypted and never reads
 * back; everything else goes to `config`, which is visible to anyone who can
 * see the connection. A value for a field the schema does not declare is
 * dropped rather than forwarded — the schema is the whole allowlist.
 */
export function splitSubmission(
  schema: ConfigSchema,
  values: Record<string, ConfigValue>,
): { config: Record<string, ConfigValue>; credentials: Record<string, string> } {
  const config: Record<string, ConfigValue> = {};
  const credentials: Record<string, string> = {};
  for (const field of schema.fields) {
    const value = values[field.name];
    if (value === undefined || value === "") continue;
    if (field.secret) {
      const secret = String(value);
      if (secret !== "") credentials[field.name] = secret;
      continue;
    }
    config[field.name] = value;
  }
  return { config, credentials };
}
