import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../app/globals.css";
import { createRoot } from "react-dom/client";
import ApiKeysPage from "../app/(app)/api-keys/page";
import { Providers } from "../components/providers";
import { WorkspaceProvider } from "../lib/workspace-context";

// Playwright intercepts all APIs. The page, dialog, permission tree and query
// hooks remain the production implementations, including their focus effects.
createRoot(document.getElementById("root")!).render(
  <Providers>
    <WorkspaceProvider
      user={{ id: "fixture-user", email: "fixture@example.invalid", display_name: "Fixture User", created_at: "2026-09-10T00:00:00Z" }}
      workspace={{ workspace_id: "fixture-workspace", workspace_name: "Browser fixture", workspace_slug: "browser-fixture", role: "member" }}
    >
      <main className="min-h-dvh bg-bg"><ApiKeysPage /></main>
    </WorkspaceProvider>
  </Providers>,
);
