"use client";

/** One approvals-inbox card (plan 17.11). Pure presentational so it can be
 * rendered in vitest without app providers. */

import { Check, X } from "lucide-react";
import Link from "next/link";
import { Badge, Button, focusRing } from "@/components/ui";
import { formatDateTime } from "@/lib/format";
import { riskTone } from "@/lib/policy";
import type { Approval } from "@/lib/types";

const STATUS_TONES: Record<string, "neutral" | "ok" | "warn" | "danger" | "accent"> = {
  pending: "warn",
  approved: "ok",
  rejected: "danger",
  cancelled: "neutral",
};

export function ApprovalCard({
  approval,
  canDecide,
  deciding,
  onApprove,
  onReject,
}: {
  approval: Approval;
  canDecide: boolean;
  deciding?: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const payload = approval.action_payload_sanitized;
  const risk = typeof payload.risk === "string" ? payload.risk : null;
  const input = payload.input;
  const pending = approval.status === "pending";

  return (
    <li
      data-testid={`approval-${approval.id}`}
      className="rounded-2xl border border-line bg-surface p-5 shadow-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-ink">
              {approval.agent_name ?? "Unknown agent"}
            </span>
            <span className="text-xs text-dim">wants to run</span>
            <code className="font-mono text-[13px] text-ink">{approval.action_type}</code>
            {risk ? <Badge tone={riskTone(risk)}>{risk}</Badge> : null}
            <Badge tone={STATUS_TONES[approval.status] ?? "neutral"}>{approval.status}</Badge>
          </div>
          {approval.reason ? (
            <p className="mt-1.5 text-sm text-dim">{approval.reason}</p>
          ) : null}
        </div>
        {pending && canDecide ? (
          <div className="flex shrink-0 gap-2">
            <Button size="sm" variant="primary" disabled={deciding} onClick={onApprove}>
              <Check size={13} aria-hidden /> Approve
            </Button>
            <Button size="sm" variant="danger" disabled={deciding} onClick={onReject}>
              <X size={13} aria-hidden /> Reject
            </Button>
          </div>
        ) : null}
      </div>

      {input !== undefined && input !== null ? (
        <pre className="mt-3 overflow-x-auto rounded-xl border border-line bg-raised px-3.5 py-2.5 font-mono text-xs leading-relaxed text-dim">
          {JSON.stringify(input, null, 2)}
        </pre>
      ) : null}

      <footer className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-faint">
        <span>Requested {formatDateTime(approval.requested_at)}</span>
        {approval.decided_at ? <span>Decided {formatDateTime(approval.decided_at)}</span> : null}
        {approval.task_id ? (
          <Link
            href={`/tasks/${approval.task_id}`}
            className={`ml-auto rounded-md font-medium text-accent-strong hover:underline ${focusRing}`}
          >
            {approval.task_title ? `Task: ${approval.task_title}` : "View task"}
          </Link>
        ) : null}
      </footer>
    </li>
  );
}
