/**
 * Turning an organization capability bundle on or off, in the one place both
 * surfaces call: the agent drawer's Tools & Access tab and the in-chat
 * composer controls.
 *
 * Connector bundles (GitHub, Web, Code editing) are planned and written by the
 * server through the bundle endpoints, because they need a connection chosen.
 * Organization bundles have nothing to choose, so they stay the client loop
 * the tab has always had: add the grants the preset needs plus any policy rule
 * it depends on, or revoke the grants it owns that no other bundle still on
 * needs. Keeping it here means the two surfaces can't drift on what
 * "turning it off" leaves behind.
 */

import { api } from "@/lib/api";
import type { ConnectionInfo, Grant, PolicyRule, ToolInfo } from "@/lib/types";
import {
  isPresetGranted,
  missingPolicyRules,
  presetGrantsToAdd,
  presetGrantsToRevoke,
  TOOL_PRESETS,
  type ToolPreset,
} from "@/lib/wizard";

export interface OrganizationBundleToggle {
  workspaceId: string;
  agentId: string;
  preset: ToolPreset;
  /** The agent's current grants, tool catalog, connections and policy rules —
   * passed in so callers reuse the queries they already hold. */
  grants: Grant[];
  tools: ToolInfo[];
  connections: ConnectionInfo[];
  rules: PolicyRule[];
}

/** Flip one organization bundle for an agent. Resolves once every grant (and
 * any policy rule the preset depends on) has been written. */
export async function toggleOrganizationBundle({
  workspaceId,
  agentId,
  preset,
  grants,
  tools,
  connections,
  rules,
}: OrganizationBundleToggle): Promise<void> {
  const base = `/api/v1/workspaces/${workspaceId}/agents/${agentId}`;
  if (isPresetGranted(grants, preset, tools)) {
    const keep = TOOL_PRESETS.filter(
      (other) => other.id !== preset.id && isPresetGranted(grants, other, tools),
    );
    for (const grant of presetGrantsToRevoke(grants, preset, tools, keep)) {
      await api<void>(`${base}/grants/${grant.id}`, { method: "DELETE" });
    }
    return;
  }
  for (const body of presetGrantsToAdd(grants, preset, tools, connections)) {
    await api(`${base}/grants`, { method: "POST", body });
  }
  const missing = missingPolicyRules(rules, preset);
  if (missing.length > 0) {
    await api(`${base}/policy`, { method: "PUT", body: { rules: [...missing, ...rules] } });
  }
}
