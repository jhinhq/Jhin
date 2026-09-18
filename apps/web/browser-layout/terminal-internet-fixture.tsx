import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "../app/globals.css";
import { createRoot } from "react-dom/client";
import { Providers } from "../components/providers";
import { TerminalInternetControl } from "../components/org/terminal-internet-control";

// The production control and API client. Browser verification maps only
// fixture IDs/authentication to the local test agent, then restores access.
createRoot(document.getElementById("root")!).render(
  <Providers>
    <main className="min-h-dvh bg-bg p-4 text-ink">
      <div className="mx-auto max-w-lg">
        <h1 className="mb-4 font-display text-xl font-semibold">Bisby · Tools &amp; Access</h1>
        <TerminalInternetControl workspaceId="browser-workspace" agentId="browser-agent" onUpdated={() => {}} />
      </div>
    </main>
  </Providers>,
);
