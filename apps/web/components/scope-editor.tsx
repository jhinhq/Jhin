"use client";

import { Field, Input, Select } from "@/components/ui";
import type { ConnectionInfo, ToolInfo } from "@/lib/types";
import type { ToolScopeValues } from "@/lib/connectors";

const LABELS: Record<string, string> = {
  connection_id: "Connection",
  project_id: "Project ID",
  deployment_id: "Deployment ID",
  environment: "Environment",
  project_ref: "Project reference",
  schema: "Schema",
  function_slug: "Function slug",
  repository: "Repository",
  branch: "Branch",
  ref: "Git ref",
  repository_id: "Repository ID",
  command: "Command pattern",
  image: "Image",
  network: "Network",
  method: "Method",
  path: "Path pattern",
  tool: "Tool pattern",
};

export function ScopeEditor({
  tool,
  connections,
  values,
  onChange,
}: {
  tool: ToolInfo;
  connections: ConnectionInfo[];
  values: ToolScopeValues;
  onChange: (values: ToolScopeValues) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {tool.scope_keys.map((key) => {
        const required = tool.required_grant_scope_keys.includes(key);
        const hint = required ? "Required for this tool" : "Optional grant constraint";
        const update = (value: string) => onChange({ ...values, [key]: value });
        return (
          <Field key={key} label={LABELS[key] ?? key.replaceAll("_", " ")} hint={hint}>
            {key === "connection_id" ? (
              <Select
                aria-label={`${LABELS[key]}${required ? " — Required for this tool" : ""}`}
                value={values[key] ?? ""}
                onChange={(event) => update(event.target.value)}
                required={required}
              >
                <option value="">Choose a connection…</option>
                {connections.map((connection) => (
                  <option key={connection.id} value={connection.id}>{connection.name}</option>
                ))}
              </Select>
            ) : key === "network" ? (
              <Select
                aria-label={`${LABELS[key]}${required ? " — Required for this tool" : ""}`}
                value={values[key] ?? ""}
                onChange={(event) => update(event.target.value)}
                required={required}
              >
                <option value="">Any network</option>
                <option value="none">none (isolated)</option>
                <option value="internet">internet (sandbox bridge)</option>
              </Select>
            ) : (
              <Input
                aria-label={`${LABELS[key] ?? key}${required ? " — Required for this tool" : ""}`}
                value={values[key] ?? ""}
                onChange={(event) => update(event.target.value)}
                required={required}
              />
            )}
          </Field>
        );
      })}
    </div>
  );
}
