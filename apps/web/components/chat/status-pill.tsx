"use client";

/** Small live status pill (text + color, never color alone). */

import { statusLabelFor, type LiveStatus, type LiveStatusSource } from "@/lib/chat";

const TONES: Record<LiveStatus["tone"], string> = {
  accent: "bg-accent-soft text-accent-strong border-accent/30",
  neutral: "bg-hover text-dim border-line-strong",
  warn: "bg-warn/10 text-warn border-warn/30",
};

export function LiveStatusPill({
  conversation,
  className = "",
}: {
  conversation: LiveStatusSource;
  className?: string;
}) {
  const status = statusLabelFor(conversation);
  if (!status) return null;
  return (
    <span
      data-testid="live-status"
      data-kind={status.kind}
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONES[status.tone]} ${className}`}
    >
      {status.kind === "working" ? (
        <span
          aria-hidden
          className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
        />
      ) : null}
      {/* An activity sentence is longer than "Working…" and this pill lives in
       * tight rows, so a truncated one is still readable on hover. */}
      <span className="truncate" title={status.specific ? status.label : undefined}>
        {status.label}
      </span>
    </span>
  );
}
