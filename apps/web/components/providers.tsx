"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api";

/** The API's answer when the workspace in the URL no longer exists, or the
 * signed-in account is no longer a member of it. */
const WORKSPACE_GONE = "Workspace not found";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => {
    let created: QueryClient | null = null;
    const queryCache = new QueryCache({
      onError: (error) => {
        // Without this the app keeps polling a workspace that has been
        // deleted (or that this account has been removed from) behind a
        // stale screen, and only a manual reload recovers. Identity is what
        // chooses the workspace, so re-reading it moves the shell on to the
        // right screen by itself.
        if (error instanceof ApiError && error.status === 404 && error.detail === WORKSPACE_GONE) {
          void created?.invalidateQueries({ queryKey: ["identity"] });
        }
      },
    });
    created = new QueryClient({
      queryCache,
      defaultOptions: {
        queries: {
          staleTime: 10_000,
          retry: (failureCount, error) => {
            // Never retry auth/permission failures; retry network blips once.
            if (error instanceof ApiError && error.status < 500) return false;
            return failureCount < 1;
          },
        },
      },
    });
    return created;
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
