import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../app/globals.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { GiveToAgent } from "../components/connection-detail";
import { ConnectionAccessSummary } from "../components/connection-access-summary";
import { Dialog } from "../components/ui";
import { useConnectionAccessSummary } from "../lib/hooks";
import { assignmentConnection, assignmentWorkspaceId } from "./assignment-data";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
});

function AssignmentFixture() {
  const summary = useConnectionAccessSummary(assignmentWorkspaceId, assignmentConnection.id);
  return (
    <Dialog title="Supabase" open wide onClose={() => {}}>
      <div className="space-y-5">
        <GiveToAgent workspaceId={assignmentWorkspaceId} connection={assignmentConnection} />
        {summary.data ? <section data-testid="assignment-summary"><ConnectionAccessSummary summary={summary.data} /></section> : null}
      </div>
    </Dialog>
  );
}

// Use the same wide Dialog and padding as ConnectionDetailDialog. Tests intercept
// every API request, so this fixture can never grant access to a real agent.
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <AssignmentFixture />
  </QueryClientProvider>,
);
