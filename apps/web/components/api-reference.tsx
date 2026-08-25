"use client";

/** The shared primitives the API reference is built from: the method badge, a
 * copy-to-clipboard button, and the tiny markdown renderer for the prose the
 * OpenAPI document carries. The navigable page and its collapsible operation
 * rows live in `components/api-docs/`; everything they show still comes out of
 * the document the API generated for itself (lib/openapi.ts). */

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { focusRing } from "@/components/ui";
import { parseMarkdown } from "@/lib/openapi";
import type { Block, Inline } from "@/lib/openapi";

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

function InlineRun({ tokens }: { tokens: Inline[] }) {
  return (
    <>
      {tokens.map((token, index) => {
        switch (token.kind) {
          case "code":
            return (
              <code
                key={index}
                className="rounded bg-hover px-1 py-0.5 font-mono text-[0.85em] text-ink"
              >
                {token.text}
              </code>
            );
          case "strong":
            return (
              <strong key={index} className="font-semibold text-ink">
                {token.text}
              </strong>
            );
          case "em":
            return <em key={index}>{token.text}</em>;
          case "link":
            return (
              <a
                key={index}
                href={token.href}
                className={`text-accent-strong underline underline-offset-2 ${focusRing}`}
                rel="noreferrer noopener"
                target="_blank"
              >
                {token.text}
              </a>
            );
          default:
            return <span key={index}>{token.text}</span>;
        }
      })}
    </>
  );
}

export function Markdown({ source, className = "" }: { source: string; className?: string }) {
  const blocks: Block[] = parseMarkdown(source);
  return (
    <div className={`space-y-3 text-sm leading-relaxed text-dim ${className}`}>
      {blocks.map((block, index) => {
        if (block.kind === "heading") {
          return (
            <h3 key={index} className="pt-2 font-display text-sm font-semibold text-ink">
              {block.text}
            </h3>
          );
        }
        if (block.kind === "code") {
          return (
            <pre
              key={index}
              className="overflow-x-auto rounded-xl bg-hover p-3 font-mono text-[12px] leading-relaxed text-ink"
            >
              <code>{block.text}</code>
            </pre>
          );
        }
        if (block.kind === "list") {
          return (
            <ul key={index} className="list-disc space-y-1 pl-5">
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <InlineRun tokens={item} />
                </li>
              ))}
            </ul>
          );
        }
        return (
          <p key={index}>
            <InlineRun tokens={block.inline} />
          </p>
        );
      })}
    </div>
  );
}
