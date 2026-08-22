"use client";

/** Audit log (plan 17.12): filterable, append-only, admin-only. */

import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
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
        <PageBody>
          <EmptyState
            title="Admins only"
            description="The audit log is visible to workspace admins and owners."
          />
        </PageBody>
      </>
    );
  }

  const page = query.data;
  const total = page?.total ?? 0;

  return (
    <>
      <PageHeader
        title="Audit"
        description="A permanent record of who changed what, and when."
      />
      <PageBody className="space-y-4">
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
            <div className="overflow-x-auto rounded-2xl border border-line bg-surface shadow-card">
              <table className="w-full min-w-[640px] text-sm">
                <thead className="text-left text-xs font-medium uppercase tracking-wider text-faint">
                  <tr>
                    <th className="px-4 py-3">Time</th>
                    <th className="px-4 py-3">Actor</th>
                    <th className="px-4 py-3">Action</th>
                    <th className="px-4 py-3">Target</th>
                    <th className="px-4 py-3">Request</th>
                  </tr>
                </thead>
                <tbody>
                  {page.events.map((event) => (
                    <tr key={event.id} className="border-t border-line hover:bg-hover">
                      <td className="whitespace-nowrap px-4 py-3 text-xs tabular-nums text-dim">
                        {formatDateTime(event.created_at)}
                      </td>
                      <td className="px-4 py-3">
                        <Badge tone={actorTone(event.actor_type)}>{event.actor_type}</Badge>
                      </td>
                      <td className="px-4 py-3">
                        <code className="font-mono text-[13px]">{event.action}</code>
                      </td>
                      <td className="px-4 py-3 text-xs text-dim">
                        {event.target_type}
                        <span className="text-faint"> · {shortId(event.target_id)}</span>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-faint">
                        {shortId(event.request_id)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between text-sm text-dim">
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
      </PageBody>
    </>
  );
}
