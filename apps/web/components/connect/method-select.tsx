"use client";

import { Select } from "@/components/ui";
import { appConnectionMethods, type AppConnectMethod } from "@/lib/apps";
import type { CatalogApp, ConnectorInfo } from "@/lib/types";

export function ConnectionMethodSelect({ entry, connectors, value, onChange }: {
  entry: CatalogApp;
  connectors: ConnectorInfo[];
  value: AppConnectMethod;
  onChange: (method: AppConnectMethod) => void;
}) {
  const methods = appConnectionMethods(entry, connectors);
  if (methods.length < 2) return null;
  return <Select className="min-w-0 max-w-full" aria-label={`Connection method for ${entry.name}`} value={value}
    onChange={(event) => onChange(event.target.value as AppConnectMethod)}>
    {methods.map((method) => <option key={method.value} value={method.value}>{method.label}</option>)}
  </Select>;
}
