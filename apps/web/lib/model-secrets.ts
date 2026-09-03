/** The one way a provider's API key reaches storage: once, in the encrypted
 * secret store, with only the secret's id ever written on the provider row. */

import { api, ApiError } from "@/lib/api";

/** Save an API key under `baseName`, picking a numbered variant when a
 * secret with that name already exists (e.g. a provider deleted and re-added
 * with the same display name). Users never chose the name, so a conflict
 * should not surface as an error. */
export async function storeApiKey(
  workspaceId: string,
  baseName: string,
  value: string,
): Promise<string> {
  for (let attempt = 1; attempt <= 20; attempt += 1) {
    const name = attempt === 1 ? baseName : `${baseName} (${attempt})`;
    try {
      const secret = await api<{ id: string }>(`/api/v1/workspaces/${workspaceId}/secrets`, {
        method: "POST",
        body: { name, value, type: "api_key" },
      });
      return secret.id;
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error;
    }
  }
  throw new ApiError(409, "Too many secrets share this name. Rename the provider and try again.");
}
