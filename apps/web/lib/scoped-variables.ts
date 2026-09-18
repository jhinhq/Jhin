import { api } from "@/lib/api";

export type VariableScope = "agent" | "team" | "company";
interface VariableMetadata {
  id: string; workspace_id: string; name: string; scope: VariableScope; scope_id: string;
  configured: boolean; version: number; description: string | null;
  created_by_type: string; created_by_id: string | null; created_at: string; updated_at: string;
  source_variable_id?: string | null; source_version?: number | null;
}
export type ScopedVariable = VariableMetadata & ({ sensitive: true; value?: never } | { sensitive: false; value: string });
export interface VariablePage { items: ScopedVariable[]; total: number; }
export const variableBase = (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/variables`;
/** Cache only the public contract, even if an older server includes extra fields. */
export function publicVariable(item: ScopedVariable): ScopedVariable {
  const { id, workspace_id, name, scope, scope_id, configured, version, description, created_by_type, created_by_id, created_at, updated_at, source_variable_id, source_version } = item;
  const metadata = { id, workspace_id, name, scope, scope_id, configured, version, description, created_by_type, created_by_id, created_at, updated_at, source_variable_id, source_version };
  return item.sensitive ? { ...metadata, sensitive: true } : { ...metadata, sensitive: false, value: item.value };
}
export async function listVariables(workspaceId: string, scope: VariableScope, scopeId: string, offset: number): Promise<VariablePage> {
  const page = await api<VariablePage>(variableBase(workspaceId), { params: { scope, scope_id: scopeId, limit: 100, offset } });
  return { total: page.total, items: page.items.map(publicVariable) };
}
