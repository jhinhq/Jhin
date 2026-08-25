"use client";

/** The pieces the API reference is built from: markdown prose, one endpoint
 * card, and the field tables inside it. Everything they render comes out of
 * the OpenAPI document the API generated for itself (lib/openapi.ts). */

import { Check, Copy, Link2 } from "lucide-react";
import { useState } from "react";
import { Badge, focusRing } from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import {
  bodySchema,
  curlFor,
  fieldRows,
  parseMarkdown,
  successResponse,
  typeName,
} from "@/lib/openapi";
import type { Block, Endpoint, FieldRow, Inline, Spec } from "@/lib/openapi";

const METHOD_TONE: Record<string, string> = {
  get: "bg-accent-soft text-accent-strong",
  post: "bg-ok-soft text-ok",
  put: "bg-warn-soft text-warn",
  patch: "bg-warn-soft text-warn",
  delete: "bg-danger-soft text-danger",
};

const AUTH_COPY: Record<Endpoint["auth"], { label: string; tone: BadgeTone; hint: string }> = {
  public: {
    label: "No credential",
    tone: "neutral",
    hint: "Reachable without signing in.",
  },
  session: {
    label: "Session only",
    tone: "warn",
    hint: "Browser session only — no API key reaches this, at any scope.",
  },
  "key-or-session": {
    label: "API key",
    tone: "ok",
    hint: "An API key with the scope below, or a browser session.",
  },
};

export function MethodTag({ method }: { method: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 font-mono text-[11px] font-semibold uppercase ${
        METHOD_TONE[method] ?? "bg-hover text-dim"
      }`}
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

function FieldTable({ rows, caption }: { rows: FieldRow[]; caption: string }) {
  if (rows.length === 0) return null;
  return (
    <div className="mt-3">
      <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-faint">{caption}</p>
      <div className="overflow-x-auto rounded-xl border border-line">
        <table className="w-full min-w-[520px] text-left text-[13px]">
          <tbody className="divide-y divide-line">
            {rows.map((row) => (
              <tr key={row.name} className="align-top">
                <td className="w-1/3 py-2 pl-3 pr-3">
                  <code
                    className="font-mono text-[12px] text-ink"
                    style={{ paddingLeft: `${row.depth * 12}px` }}
                  >
                    {row.name}
                  </code>
                  {row.required ? (
                    <span className="ml-1.5 text-[10px] font-semibold uppercase text-danger">
                      required
                    </span>
                  ) : null}
                </td>
                <td className="py-2 pr-3">
                  <code className="font-mono text-[12px] text-dim">{row.type}</code>
                  {row.enum ? (
                    <p className="mt-0.5 font-mono text-[11px] text-faint">
                      {row.enum.join(" | ")}
                    </p>
                  ) : null}
                </td>
                <td className="py-2 pr-3 text-dim">{row.description ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function EndpointCard({
  endpoint,
  spec,
  origin,
  workspaceId,
}: {
  endpoint: Endpoint;
  spec: Spec;
  origin: string;
  workspaceId?: string;
}) {
  const { operation } = endpoint;
  const auth = AUTH_COPY[endpoint.auth];
  const success = successResponse(operation);
  const requestRows = fieldRows(bodySchema(operation.requestBody), spec);
  const responseRows = success ? fieldRows(bodySchema(success[1]), spec) : [];
  const parameters = (operation.parameters ?? []).filter((parameter) => parameter.in !== "header");
  const curl = curlFor(endpoint, { origin, workspaceId });

  return (
    <article
      id={endpoint.id}
      data-testid="endpoint"
      className="scroll-mt-24 rounded-2xl border border-line bg-surface p-5 shadow-card"
    >
      <div className="flex flex-wrap items-center gap-2">
        <MethodTag method={endpoint.method} />
        <code className="min-w-0 flex-1 break-all font-mono text-[13px] text-ink">
          {endpoint.path}
        </code>
        <a
          href={`#${endpoint.id}`}
          aria-label={`Link to ${endpoint.method.toUpperCase()} ${endpoint.path}`}
          className={`rounded-md p-1 text-faint hover:text-ink ${focusRing}`}
        >
          <Link2 size={13} aria-hidden />
        </a>
      </div>

      <h3 className="mt-2 font-display text-base font-semibold text-ink">{endpoint.title}</h3>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Badge tone={auth.tone}>{auth.label}</Badge>
        {endpoint.scope ? (
          <code className="rounded-md bg-hover px-1.5 py-0.5 font-mono text-[11px] text-dim">
            {endpoint.scope}
          </code>
        ) : null}
        {operation.deprecated ? <Badge tone="danger">Deprecated</Badge> : null}
        <span className="text-xs text-faint">{auth.hint}</span>
      </div>

      {operation.description ? <Markdown source={operation.description} className="mt-3" /> : null}

      {parameters.length > 0 ? (
        <FieldTable
          caption="Parameters"
          rows={parameters.map((parameter) => ({
            name: parameter.name,
            type: `${typeName(parameter.schema, spec)} · ${parameter.in}`,
            required: Boolean(parameter.required),
            description: parameter.description ?? null,
            enum: null,
            depth: 0,
          }))}
        />
      ) : null}

      <FieldTable caption="Request body" rows={requestRows} />
      <FieldTable
        caption={success ? `Response · ${success[0]}` : "Response"}
        rows={responseRows}
      />

      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between">
          <p className="text-xs font-semibold uppercase tracking-wider text-faint">Example</p>
          <CopyButton value={curl} label={`Copy the curl example for ${endpoint.title}`} />
        </div>
        <pre className="overflow-x-auto rounded-xl bg-hover p-3 font-mono text-[12px] leading-relaxed text-ink">
          <code>{curl}</code>
        </pre>
      </div>
    </article>
  );
}
