"use client";

/** The generic Connect form, drawn from a normalised schema.
 *
 * One labelled control per field, so somebody using a screen reader meets the
 * same form as everybody else. The schema decides which control appears; it
 * never decides what a control *is* — the four types here are the whole
 * vocabulary, and `normalizeConfigSchema` has already reduced anything else
 * to a text input before this component sees it.
 *
 * Every string the schema carries is rendered as text by React, which escapes
 * it. A help string is help text and nothing more, whatever it contains.
 */

import { Field, Input, Select, Textarea } from "@/components/ui";
import type { ConfigSchema, ConfigSchemaField, ConfigValue } from "@/lib/config-schema";

/** A secret is never rendered with a value in it, so this is the only thing
 * the form can honestly say about one. */
const SECRET_HINT = "Stored encrypted (AES-256-GCM envelope); never displayed again.";

function asText(value: ConfigValue | undefined): string {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.join("\n");
  if (typeof value === "boolean") return value ? "true" : "";
  return String(value);
}

function SchemaControl({
  field,
  value,
  disabled,
  onChange,
}: {
  field: ConfigSchemaField;
  value: ConfigValue | undefined;
  disabled: boolean;
  onChange: (name: string, value: ConfigValue) => void;
}) {
  const shared = {
    "aria-label": field.label,
    disabled,
    required: field.required,
    placeholder: field.placeholder || undefined,
  };

  if (field.type === "boolean") {
    return (
      <input
        {...shared}
        type="checkbox"
        checked={value === true}
        onChange={(event) => onChange(field.name, event.target.checked)}
      />
    );
  }

  if (field.secret) {
    return (
      <Input
        {...shared}
        type="password"
        autoComplete="off"
        maxLength={field.max_length ?? undefined}
        value={asText(value)}
        onChange={(event) => onChange(field.name, event.target.value)}
      />
    );
  }

  // An enum only reaches here when the normaliser honoured it, so the options
  // are exactly what the server offered — no synthetic blank choice, which
  // would be a value the schema never described.
  if (field.enum.length > 0) {
    return (
      <Select
        {...shared}
        value={asText(value)}
        onChange={(event) => onChange(field.name, event.target.value)}
      >
        {field.enum.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </Select>
    );
  }

  if (field.type === "string_list" || field.multiline) {
    return (
      <Textarea
        {...shared}
        rows={3}
        maxLength={field.max_length ?? undefined}
        value={asText(value)}
        onChange={(event) =>
          onChange(
            field.name,
            field.type === "string_list"
              ? event.target.value.split("\n").filter((line) => line.trim() !== "")
              : event.target.value,
          )
        }
      />
    );
  }

  return (
    <Input
      {...shared}
      type={field.type === "integer" ? "number" : "text"}
      min={field.minimum ?? undefined}
      max={field.maximum ?? undefined}
      maxLength={field.max_length ?? undefined}
      value={asText(value)}
      onChange={(event) =>
        onChange(
          field.name,
          field.type === "integer" && event.target.value !== ""
            ? Number(event.target.value)
            : event.target.value,
        )
      }
    />
  );
}

export function SchemaForm({
  schema,
  values,
  onChange,
  disabled = false,
}: {
  schema: ConfigSchema;
  values: Record<string, ConfigValue>;
  onChange: (name: string, value: ConfigValue) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-4" data-testid="schema-form">
      {schema.fields.map((field) => (
        <Field
          key={field.name}
          label={field.label}
          hint={field.secret ? SECRET_HINT : field.help || undefined}
        >
          <SchemaControl
            field={field}
            value={values[field.name]}
            disabled={disabled}
            onChange={onChange}
          />
        </Field>
      ))}
    </div>
  );
}
