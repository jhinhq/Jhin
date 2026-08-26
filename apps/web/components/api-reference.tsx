"use client";

/** The shared primitives the API reference is built from: the method badge and
 * a copy-to-clipboard button. The prose the OpenAPI document carries is
 * markdown, rendered by `components/markdown` — one renderer shared with the
 * chat transcript, so its link allow-list covers the docs too. The navigable
 * page and its collapsible operation rows live in `components/api-docs/`;
 * everything they show still comes out of the document the API generated for
 * itself (lib/openapi.ts). */

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { focusRing } from "@/components/ui";

/** Method → colour. GET reads (accent), POST creates (ok/green), PUT and PATCH
 * change (warn/amber), DELETE removes (danger/red). Kept as a lookup so the
 * same palette drives the badge here and any legend that documents it. */
export const METHOD_TONE: Record<string, string> = {
  get: "bg-accent-soft text-accent-strong",
  post: "bg-ok-soft text-ok",
  put: "bg-warn-soft text-warn",
  patch: "bg-warn-soft text-warn",
  delete: "bg-danger-soft text-danger",
};

export function MethodTag({ method, className = "" }: { method: string; className?: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center rounded-md px-1.5 py-0.5 font-mono text-[11px] font-semibold uppercase ${
        METHOD_TONE[method] ?? "bg-hover text-dim"
      } ${className}`}
    >
      {method}
    </span>
  );
}

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={label}
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-faint transition-colors hover:bg-hover hover:text-ink ${focusRing}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
