import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../app/globals.css";
import { createRoot } from "react-dom/client";
import ChatThreadPage from "../app/(app)/chats/[id]/view";
import { Providers } from "../components/providers";
import { WorkspaceProvider } from "../lib/workspace-context";

// Keep the production query defaults, page, transcript, composer and hooks.
// Playwright intercepts every API call before loading this fixture.
createRoot(document.getElementById("root")!).render(
  <Providers>
    <WorkspaceProvider
      user={{ id: "browser-user", email: "fixture@example.invalid", display_name: "Dev Owner", created_at: "2026-09-10T00:00:00Z" }}
      workspace={{ workspace_id: "browser-workspace", workspace_name: "Browser fixture", workspace_slug: "browser-fixture", role: new URLSearchParams(window.location.search).get("role") === "admin" ? "admin" : "member" }}
    >
      <main className="flex h-dvh min-h-0 flex-col overflow-hidden bg-bg">
        <ChatThreadPage />
      </main>
    </WorkspaceProvider>
  </Providers>,
);
