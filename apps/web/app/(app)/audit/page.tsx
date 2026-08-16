"use client";

/** Audit log (plan 17.12): filterable, append-only, admin-only. */

import { useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { Badge, Button, EmptyState, Field, Input, Select, Spinner } from "@/components/ui";
import { formatDateTime, shortId } from "@/lib/format";
import { useAuditEvents } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const PAGE_SIZE = 50;

const TARGET_TYPES = ["", "workspace", "workspace_membership", "team", "agent", "user"];

function actorTone(actorType: string): "accent" | "neutral" | "warn" {
  if (actorType === "user") return "accent";
  if (actorType === "agent") return "warn";
  return "neutral";
}

export default function AuditPage() {
  const { workspace, can } = useWorkspace();
  const [action, setAction] = useState("");
  const [targetType, setTargetType] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [offset, setOffset] = useState(0);

  const isAdmin = can("admin");
  const query = useAuditEvents(
    workspace.workspace_id,
    {
      action: action || undefined,
      target_type: targetType || undefined,
      created_from: createdFrom ? new Date(createdFrom).toISOString() : undefined,
      created_to: createdTo ? new Date(createdTo).toISOString() : undefined,
      limit: PAGE_SIZE,
      offset,
    },
    isAdmin,
  );

  if (!isAdmin) {
    return (
      <>
        <PageHeader title="Audit" />
        <div className="px-8 py-6">
          <EmptyState
            title="Admins only"
            description="The audit log is visible to workspace admins and owners."
          />
        </div>
      </>
    );
  }

  const page = query.data;
  const total = page?.total ?? 0;

  return (
    <>
      <PageHeader
        title="Audit"
        description="Append-only record of configuration and auth events"
      />
      <div className="space-y-4 px-8 py-6">
        <form
          className="grid grid-cols-2 items-end gap-3 lg:grid-cols-5"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            void query.refetch();
          }}
        >
          <Field label="Action">
            <Input
              value={action}
              onChange={(e) => {
                setAction(e.target.value);
                setOffset(0);
              }}
              placeholder="agent.created"
            />
          </Field>
          <Field label="Target type">
            <Select
              value={targetType}
              onChange={(e) => {
                setTargetType(e.target.value);
                setOffset(0);
              }}
            >
              {TARGET_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type || "All"}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="From">
            <Input
              type="datetime-local"
              value={createdFrom}
              onChange={(e) => {
                setCreatedFrom(e.target.value);
                setOffset(0);
              }}
            />
          </Field>
          <Field label="To">
            <Input
              type="datetime-local"
              value={createdTo}
              onChange={(e) => {
                setCreatedTo(e.target.value);
                setOffset(0);
              }}
            />
          </Field>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setAction("");
              setTargetType("");
              setCreatedFrom("");
              setCreatedTo("");
              setOffset(0);
            }}
          >
            Clear filters
          </Button>
        </form>

        {query.isPending ? (
          <Spinner label="Loading audit log…" />
        ) : page && page.events.length > 0 ? (
          <>
            <div className="overflow-hidden rounded-xl border border-line">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wider text-dim">
                    <th className="px-4 py-2.5 font-medium">Time</th>
                    <th className="px-4 py-2.5 font-medium">Actor</th>
                    <th className="px-4 py-2.5 font-medium">Action</th>
                    <th className="px-4 py-2.5 font-medium">Target</th>
                    <th className="px-4 py-2.5 font-medium">Request</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {page.events.map((event) => (
                    <tr key={event.id} className="hover:bg-surface">
                      <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-dim">
                        {formatDateTime(event.created_at)}
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge tone={actorTone(event.actor_type)}>{event.actor_type}</Badge>
                      </td>
                      <td className="px-4 py-2.5">
                        <code className="text-[13px]">{event.action}</code>
                      </td>
                      <td className="px-4 py-2.5 text-xs text-dim">
                        {event.target_type}
                        <span className="text-faint"> · {shortId(event.target_id)}</span>
                      </td>
                      <td className="px-4 py-2.5 text-xs text-faint">
                        {shortId(event.request_id)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between text-xs text-dim">
              <span className="tabular-nums">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
              </span>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </Button>
                <Button
                  size="sm"
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        ) : (
          <EmptyState
            title="No matching audit events"
            description="Try widening the filters. Every configuration and auth change is recorded here."
          />
        )}
      </div>
    </>
  );
}
