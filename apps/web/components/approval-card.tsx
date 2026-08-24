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

// skills.create/skills.update carry a full skill body (up to 64 KB) in
// `input.content`; the raw JSON dump reads poorly on an approval card, so
// these two actions get a compact name + short preview instead
// (docs/architecture/skills.md, "Approval card readability").
const SKILL_CONTENT_PREVIEW_ACTIONS = new Set(["skills.create", "skills.update"]);
const SKILL_PREVIEW_CHARS = 200;

function skillContentPreview(input: unknown): { name: string; preview: string } | null {
  if (typeof input !== "object" || input === null) return null;
  const record = input as Record<string, unknown>;
  const name = typeof record.name === "string" ? record.name : null;
  const content = typeof record.content === "string" ? record.content : null;
  if (name === null && content === null) return null;
  const preview =
    content === null
      ? ""
      : content.length > SKILL_PREVIEW_CHARS
        ? `${content.slice(0, SKILL_PREVIEW_CHARS)}…`
        : content;
  return { name: name ?? "(unchanged)", preview };
}

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
  const skillPreview = SKILL_CONTENT_PREVIEW_ACTIONS.has(approval.action_type)
    ? skillContentPreview(input)
    : null;

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

      {skillPreview ? (
        <div className="mt-3 rounded-xl border border-line bg-raised px-3.5 py-2.5">
          <code className="font-mono text-xs font-medium text-ink">{skillPreview.name}</code>
          {skillPreview.preview ? (
            <p className="mt-1.5 whitespace-pre-wrap font-mono text-xs leading-relaxed text-dim">
              {skillPreview.preview}
            </p>
          ) : null}
        </div>
      ) : input !== undefined && input !== null ? (
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
