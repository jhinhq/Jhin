"use client";

/** Create-connection dialog used across Apps: the library pre-fills name,
 * auth scheme, and config from a catalog entry, while the service-type
 * gallery opens it blank. Credentials are write-only.
 *
 * Two ways to draw the config half. By default the fields come from the
 * installed connector manifest, which is what every path did before the
 * catalog. When a `schema` is supplied — the server's render contract for one
 * catalog entry, built from those same manifests — it replaces that half, so
 * the entry's prefilled values and bounds reach the form without the catalog
 * ever describing a field that does not exist. The auth half is the manifest's
 * either way: secrets are the connector's business, not the index's.
 *
 * Since OAuth landed this is the *fallback*, not the front door. `ConnectPanel`
 * decides how an app is connected and only renders this when there is no
 * sign-in flow to use, or when somebody deliberately took the "use an API key
 * instead" link. `note` is how the panel says so, and `onBack` is how the
 * person changes their mind — neither is required, so every existing caller
 * (the service-type gallery, the catalog sheet) is untouched. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import {
  Button,
  Dialog,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Select,
  Textarea,
} from "@/components/ui";
import { SchemaForm } from "@/components/schema-form";
import { api, ApiError } from "@/lib/api";
import {
  initialValuesFor,
  splitSubmission,
  validateConfigValues,
  type ConfigSchema,
  type ConfigValue,
} from "@/lib/config-schema";
import {
  coerceConnectorConfig,
  configFieldsForAuth,
  findAuthScheme,
  validateConnectionForm,
} from "@/lib/connectors";
import type { ConnectionCreated, ConnectorInfo } from "@/lib/types";

function errText(error: unknown, fallback: string): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.detail : fallback;
}

export interface ConnectionPrefill {
  name?: string;
  authType?: string;
  config?: Record<string, string | boolean>;
  /** Friendly guidance shown at the top of the form. */
  hint?: string | null;
}

export function CreateConnectionDialog({
  workspaceId,
  connector,
  prefill,
  schema,
  note,
  onBack,
  onClose,
  onCreated,
}: {
  workspaceId: string;
  connector: ConnectorInfo;
  prefill?: ConnectionPrefill;
  /** The server's render contract for a catalog entry, when there is one. */
  schema?: ConfigSchema | null;
  /** What this form costs compared with the sign-in flow this app supports. */
  note?: string | null;
  /** Present only when there is a sign-in flow to go back to. */
  onBack?: () => void;
  onClose: () => void;
  onCreated: (created: ConnectionCreated) => void;
}) {
  const [name, setName] = useState(prefill?.name ?? "");
  const [authType, setAuthType] = useState(
    prefill?.authType && findAuthScheme(connector, prefill.authType)
      ? prefill.authType
      : connector.auth_schemes[0]?.type ?? "",
  );
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const initialConfig = (selectedAuth: string) => ({
    ...Object.fromEntries(
      configFieldsForAuth(connector, selectedAuth)
        .filter((field) => field.default !== null)
        .map((field) => [
          field.name,
          Array.isArray(field.default)
            ? field.default.join("\n")
            : typeof field.default === "boolean"
              ? field.default
              : String(field.default),
        ]),
    ),
    ...(prefill?.config ?? {}),
  });
  const [config, setConfig] = useState<Record<string, string | boolean>>(() => initialConfig(authType));
  // The schema's own values, kept beside the manifest ones rather than merged
  // into them: only one of the two halves is ever rendered or submitted.
  const [schemaValues, setSchemaValues] = useState<Record<string, ConfigValue>>(() =>
    schema ? initialValuesFor(schema) : {},
  );
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const scheme = findAuthScheme(connector, authType);

  const create = useMutation({
    mutationFn: () => {
      const typed = Object.fromEntries(
        Object.entries(credentials).filter(([, value]) => value.trim() !== ""),
      );
      const submission = schema ? splitSubmission(schema, schemaValues) : null;
      return api<ConnectionCreated>(`/api/v1/workspaces/${workspaceId}/connections`, {
        method: "POST",
        body: {
          connector_type: connector.connector_type,
          name: name.trim(),
          auth_type: authType,
          // A secret the schema declared and a secret the manifest declared
          // are the same kind of thing and go to the same place.
          credentials: { ...typed, ...(submission?.credentials ?? {}) },
          config:
            submission?.config ??
            coerceConnectorConfig(configFieldsForAuth(connector, authType), config),
        },
      });
    },
    onSuccess: onCreated,
  });

  return (
    <Dialog title={`Connect ${prefill?.name ?? connector.display_name}`} open onClose={onClose} wide>
      <form
        className="space-y-4"
        data-testid="create-connection-form"
        onSubmit={(event) => {
          event.preventDefault();
          const errors = [
            ...validateConnectionForm(connector, name, authType, credentials),
            ...(schema ? validateConfigValues(schema, schemaValues) : []),
          ];
          setFormErrors(errors);
          if (errors.length === 0) create.mutate();
        }}
      >
        {note ? (
          <p
            data-testid="api-key-fallback-note"
            className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-sm leading-relaxed text-ink"
          >
            {note}
          </p>
        ) : null}
        {prefill?.hint ? (
          <p className="rounded-xl border border-accent/30 bg-accent-soft px-3.5 py-2.5 text-sm text-ink">
            {prefill.hint}
          </p>
        ) : null}
        <Field label="Connection name" hint="Unique in this workspace.">
          <Input
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={`${connector.display_name} (production)`}
          />
        </Field>

        <Field label="Authentication">
          <Select
            value={authType}
            onChange={(e) => {
              setAuthType(e.target.value);
              setCredentials({});
              setConfig(initialConfig(e.target.value));
            }}
          >
            {connector.auth_schemes.map((s) => (
              <option key={s.type} value={s.type}>
                {s.label}
              </option>
            ))}
          </Select>
        </Field>
        {scheme?.description ? <p className="text-xs text-dim">{scheme.description}</p> : null}

        {(scheme?.secret_fields ?? []).map((field) => (
          <Field
            key={field.name}
            label={field.label}
            hint="Stored encrypted (AES-256-GCM envelope); never displayed again."
          >
            {field.multiline ? (
              <Textarea
                rows={5}
                value={credentials[field.name] ?? ""}
                onChange={(e) =>
                  setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
                placeholder={field.placeholder}
                required={field.required}
                className="font-mono text-xs"
              />
            ) : (
              <Input
                type="password"
                autoComplete="off"
                value={credentials[field.name] ?? ""}
                onChange={(e) =>
                  setCredentials((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
                placeholder={field.placeholder}
                required={field.required}
              />
            )}
          </Field>
        ))}

        {schema ? (
          <SchemaForm
            schema={schema}
            values={schemaValues}
            onChange={(field, value) =>
              setSchemaValues((prev) => ({ ...prev, [field]: value }))
            }
            disabled={create.isPending}
          />
        ) : (
          <>
          {configFieldsForAuth(connector, authType)
            .filter((field) => field.name !== "allow_writes")
            .map((field) => (
              <Field key={field.name} label={field.label} hint={field.help}>
                {field.kind === "boolean" ? (
                  <input
                    aria-label={field.label}
                    type="checkbox"
                    checked={config[field.name] === true}
                    onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.checked }))}
                  />
                ) : field.kind === "string_list" ? (
                  <Textarea
                    rows={3}
                    value={String(config[field.name] ?? "")}
                    onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.value }))}
                    placeholder={field.placeholder}
                    required={field.required}
                  />
                ) : (
                  <Input
                    aria-label={field.label}
                    type={field.kind === "integer" ? "number" : "text"}
                    min={field.minimum ?? undefined}
                    max={field.maximum ?? undefined}
                    value={String(config[field.name] ?? "")}
                    onChange={(e) => setConfig((prev) => ({ ...prev, [field.name]: e.target.value }))}
                    placeholder={field.placeholder}
                    required={field.required}
                  />
                )}
              </Field>
            ))}

          {configFieldsForAuth(connector, authType).some((field) => field.name === "allow_writes") ? (
            <details className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5">
              <summary className={`cursor-pointer rounded-md text-sm font-semibold text-ink ${focusRing}`}>Advanced database access</summary>
              <label className="mt-3 flex items-start gap-2 text-sm">
                <input
                  aria-label="Allow database writes"
                  type="checkbox"
                  checked={config.allow_writes === true}
                  onChange={(e) => setConfig((prev) => ({ ...prev, allow_writes: e.target.checked }))}
                />
                <span>
                  Allow database writes
                  <span className="block text-xs text-dim">Off by default. DDL is never available to agents.</span>
                </span>
              </label>
            </details>
          ) : null}
          </>
        )}

        {formErrors.length > 0 ? <ErrorNote message={formErrors.join(" ")} /> : null}
        <ErrorNote message={errText(create.error, "Creating the connection failed.")} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onBack ?? onClose}>
            {onBack ? "Back" : "Cancel"}
          </Button>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Connecting…" : "Create connection"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
