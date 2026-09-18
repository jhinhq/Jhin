import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "../app/globals.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { CompanyResources } from "../components/company/company-resources";
import { VariablesPanel } from "../components/variables/variables-panel";
import { SchedulesPanel } from "../components/automations/schedules-panel";
import { EditorialReviewsPanel } from "../components/editorial/reviews-panel";
import type { OrgTeamNode } from "../lib/types";

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const team = { id:"t1",name:"Marketing",parent_team_id:null,manager_agent_id:"director",description:"Editorial team",color_token:"lavender",icon:"users",created_at:"2026-09-12T00:00:00Z" } as OrgTeamNode;
function Fixture() { return <main className="mx-auto max-w-5xl space-y-8 p-4 sm:p-8"><h1 className="font-display text-2xl font-semibold">Agent work settings</h1><CompanyResources workspaceId="ws" teams={[team]} canWrite /><VariablesPanel workspaceId="ws" scope="agent" scopeId="a1" scopeName="Blogger" canWrite /><SchedulesPanel workspaceId="ws" agents={[{id:"a1",name:"Blogger"}]} canWrite /><EditorialReviewsPanel workspaceId="ws" connectionId="ghost1" /></main>; }
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={client}><Fixture /></QueryClientProvider>);
