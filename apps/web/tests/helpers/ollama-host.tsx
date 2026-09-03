/** Test-only: the page's Ollama-host subscription for a component rendered on
 * its own. Cards read the host from props — one subscription per provider,
 * made at the page (lib/ollama-host.ts) — so a card under test needs someone
 * to make it. Not a test file: vitest only collects `*.test.*`. */

import type { ReactNode } from "react";
import { useOllamaHosts, type OllamaHost } from "@/lib/ollama-host";

export function WithOllamaHost({
  workspaceId,
  providerId,
  children,
}: {
  workspaceId: string;
  providerId: string;
  children: (host: OllamaHost) => ReactNode;
}) {
  const host = useOllamaHosts(workspaceId, [providerId]).get(providerId);
  return host ? <>{children(host)}</> : null;
}
