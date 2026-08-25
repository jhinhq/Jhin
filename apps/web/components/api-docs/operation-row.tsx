"use client";

/** One endpoint, collapsed by default.
 *
 * The row a reader always sees is scannable: a colour-coded method badge, the
 * path, the summary, and the scope a key needs. Only on expand does the row
 * render the parameter / request / response tables and a runnable curl line —
 * so a tag with forty operations is forty lines, not forty walls of tables, and
 * the detail is built lazily for the handful a reader actually opens. */

import { ChevronRight, Link2 } from "lucide-react";
import { CopyButton, Markdown, MethodTag } from "@/components/api-reference";
import { Badge, focusRing } from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import {
  bodySchema,
  curlFor,
  fieldRows,
  successResponse,
  typeName,
} from "@/lib/openapi";
import type { Endpoint, FieldRow, Spec } from "@/lib/openapi";

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
                    <p className="mt-0.5 font-mono text-[11px] text-faint">{row.enum.join(" | ")}</p>
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

/** The tables and curl example a reader sees once a row is expanded. Rendered
 * only when open, so the schema flattening for 183 operations never runs up
 * front. */
function OperationDetail({
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
    <div className="border-t border-line px-4 pb-4 pt-3 sm:px-5">
      <div className="flex flex-wrap items-center gap-2">
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
      <FieldTable caption={success ? `Response · ${success[0]}` : "Response"} rows={responseRows} />

      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between">
          <p className="text-xs font-semibold uppercase tracking-wider text-faint">Example</p>
          <CopyButton value={curl} label={`Copy the curl example for ${endpoint.title}`} />
        </div>
        <pre className="overflow-x-auto rounded-xl bg-hover p-3 font-mono text-[12px] leading-relaxed text-ink">
          <code>{curl}</code>
        </pre>
      </div>
    </div>
  );
}

export function OperationRow({
  endpoint,
  spec,
  origin,
  workspaceId,
  expanded,
  onToggle,
}: {
  endpoint: Endpoint;
  spec: Spec;
  origin: string;
  workspaceId?: string;
  expanded: boolean;
  onToggle: (id: string, next: boolean) => void;
}) {
  const detailId = `${endpoint.id}-detail`;
  return (
    <article
      id={endpoint.id}
      data-testid="endpoint"
      className="scroll-mt-28 overflow-hidden rounded-xl border border-line bg-surface shadow-card transition-colors hover:border-line-strong"
    >
      <div className="flex items-stretch">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailId}
          onClick={() => onToggle(endpoint.id, !expanded)}
          className={`group flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5 text-left sm:px-4 ${focusRing}`}
        >
          <ChevronRight
            size={15}
            aria-hidden
            className={`shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
          />
          <MethodTag method={endpoint.method} className="w-16" />
          <code className="shrink-0 break-all font-mono text-[13px] text-ink">{endpoint.path}</code>
          <span className="min-w-0 flex-1 truncate text-[13px] text-dim">{endpoint.title}</span>
          {endpoint.scope ? (
            <code className="ml-auto hidden shrink-0 rounded-md bg-hover px-1.5 py-0.5 font-mono text-[11px] text-dim sm:inline">
              {endpoint.scope}
            </code>
          ) : null}
          {endpoint.operation.deprecated ? (
            <span className="shrink-0 text-[10px] font-semibold uppercase text-danger">
              deprecated
            </span>
          ) : null}
        </button>
        <a
          href={`#${endpoint.id}`}
          aria-label={`Link to ${endpoint.method.toUpperCase()} ${endpoint.path}`}
          className={`flex shrink-0 items-center px-2 text-faint hover:text-ink ${focusRing}`}
        >
          <Link2 size={13} aria-hidden />
        </a>
      </div>
      {expanded ? (
        <div id={detailId} data-testid="endpoint-detail">
          <OperationDetail
            endpoint={endpoint}
            spec={spec}
            origin={origin}
            workspaceId={workspaceId}
          />
        </div>
      ) : null}
    </article>
  );
}
