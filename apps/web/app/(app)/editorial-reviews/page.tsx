"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PageBody, PageHeader } from "@/components/app-shell";
import { EditorialReviewsPanel } from "@/components/editorial/reviews-panel";
import { Field, Select, Spinner } from "@/components/ui";
import { useConnections } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

function EditorialReviewsView() {
  const {workspace} = useWorkspace(), params = useSearchParams();
  const [chosen, setChosen] = useState(params.get("connection") ?? "");
  const connections = useConnections(workspace.workspace_id);
  const ghost = connections.data?.filter((connection) => connection.connector_type === "ghost") ?? [];
  const connectionId = chosen || ghost[0]?.id;
  return <><PageHeader title="Director reviews" description="Inspect the exact drafts, review feedback, and publication status." /><PageBody className="space-y-5 max-w-4xl"><p className="text-sm text-dim">The designated director approves and publishes through its authorized tools. Review snapshots stay available here.</p><Field label="Ghost connection"><Select value={connectionId ?? ""} onChange={(event) => setChosen(event.target.value)}>{chosen && !ghost.some((connection) => connection.id === chosen) ? <option value={chosen}>Linked connection</option> : null}{ghost.map((connection) => <option key={connection.id} value={connection.id}>{connection.name}</option>)}</Select></Field>{connections.isPending ? <Spinner /> : null}{connectionId ? <EditorialReviewsPanel key={connectionId} workspaceId={workspace.workspace_id} connectionId={connectionId} initialReviewId={params.get("review")} /> : !connections.isPending ? <p className="text-sm text-dim">Connect Ghost and ask an agent to prepare a draft for director review.</p> : null}</PageBody></>;
}
export default function EditorialReviewsPage() { return <Suspense fallback={<Spinner />}><EditorialReviewsView /></Suspense>; }
