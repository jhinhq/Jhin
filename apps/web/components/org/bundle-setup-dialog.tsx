"use client";

/** Turning a connector capability bundle on for one agent: pick the
 * connection(s), create or choose the sandbox, say which repositories, then
 * review exactly what will be written. Every step is shown, pre-filled when
 * the answer is unambiguous, and nothing is written until Review's primary
 * button posts the same body the dry run previewed. */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Badge, Button, Dialog, ErrorNote, Field, focusRing, Input, Select, Spinner, Textarea } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  activeConnectionsOf,
  allowedRepositoriesOf,
  bundleApplyBody,
  connectorLabel,
  defaultBundleOptions,
  repositoryCoveredBySandbox,
  reviewLines,
  sandboxesFor,
  sandboxRepositoryError,
  stepsFor,
  type BundleOptions,
} from "@/lib/bundles";
import { useConnectors } from "@/lib/hooks";
import type { Agent, BundleApplyOut, BundleStatusOut, ConnectionInfo } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const TOKEN_DOCS = "https://github.com/jhin-ai/jhin/blob/main/docs/operations/github-token-setup.md";

function stepLabel(step: string, connectorNames: Record<string, string>): string {
  if (step === "sandbox") return "Sandbox";
  if (step === "repositories") return "Repositories";
  if (step === "review") return "Review";
  if (step === "github") return "GitHub";
  return connectorNames[step] ?? connectorLabel(step);
}

function authLabel(connection: ConnectionInfo): string {
  return connection.auth_type === "oauth" ? "signed in" : connection.auth_type.replace("_", " ");
}

function parseLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

export function BundleSetupDialog({
  agent,
  bundle,
  connections,
  initial = {},
  onDone,
  onClose,
}: {
  agent: Pick<Agent, "id" | "name">;
  bundle: BundleStatusOut;
  connections: ConnectionInfo[];
  initial?: { connectionId?: string };
  /** The bundle is on; the caller invalidates and shows the notice. */
  onDone: (result: BundleApplyOut) => void;
  onClose: () => void;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const connectors = useConnectors();
  const connectorNames = useMemo(
    () => Object.fromEntries((connectors.data ?? []).map((c) => [c.connector_type, c.display_name])),
    [connectors.data],
  );
  const steps = useMemo(() => stepsFor(bundle), [bundle]);
  const [stepIndex, setStepIndex] = useState(0);
  const [options, setOptions] = useState<BundleOptions>(() =>
    defaultBundleOptions(bundle, connections, initial),
  );
  const [allowedText, setAllowedText] = useState("");
  const [repositoriesText, setRepositoriesText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const step = steps[stepIndex];
  // The Review step previews exactly the body the primary button will post;
  // keyed on that body, so changing an answer and coming back re-checks.
  const previewBody = step === "review" ? bundleApplyBody(bundle, options, true) : null;
  const previewQuery = useQuery({
    queryKey: ["bundle-preview", workspaceId, agent.id, bundle.id, previewBody],
    queryFn: () =>
      api<BundleApplyOut>(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/bundles/${bundle.id}`, {
        method: "POST",
        body: previewBody,
      }),
    enabled: previewBody !== null,
    retry: false,
    staleTime: 0,
  });
  const preview = previewQuery.data ?? null;
  const previewing = previewQuery.isFetching;
  const previewError = previewQuery.error
    ? previewQuery.error instanceof ApiError
      ? previewQuery.error.detail
      : `Turning on ${bundle.label} failed. Nothing was changed.`
    : null;
  const github = connections.find((c) => c.id === options.connections.github);
  const existingSandboxes = github ? sandboxesFor(connections, github.id) : [];
  const chosenSandbox = connections.find((c) => c.id === options.connections.cli);
  const sandboxName =
    options.sandboxMode === "create" ? options.sandbox.name.trim() || "the sandbox" : chosenSandbox?.name ?? "the sandbox";
  const sandboxAllowList =
    bundle.id !== "code-editing"
      ? ["*"]
      : options.sandboxMode === "create"
        ? options.sandbox.allowedMode === "any"
          ? ["*"]
          : options.sandbox.allowed
        : allowedRepositoriesOf(chosenSandbox);

  const allowedErrors = options.sandbox.allowed.map((entry) => sandboxRepositoryError(entry)).filter(Boolean);
  const repositoryErrors = options.repositories.map((entry) => {
    const shape = sandboxRepositoryError(entry);
    if (shape) return `${entry}: ${shape}`;
    if (bundle.id === "code-editing" && !repositoryCoveredBySandbox(entry, sandboxAllowList)) {
      return `${sandboxName} does not allow ${entry}. Add it to the sandbox's allowed repositories under Apps first.`;
    }
    return null;
  });

  const stepBlocked = (() => {
    if (step === "sandbox") {
      if (options.sandboxMode === "existing") return !options.connections.cli;
      return (
        !options.sandbox.name.trim() ||
        (options.sandbox.allowedMode === "list" && (options.sandbox.allowed.length === 0 || allowedErrors.length > 0))
      );
    }
    if (step === "repositories") {
      return options.repositoriesMode === "list" && (options.repositories.length === 0 || repositoryErrors.some(Boolean));
    }
    if (step !== "review") return !options.connections[step];
    return false;
  })();

  const update = (patch: Partial<BundleOptions>) => setOptions((current) => ({ ...current, ...patch }));

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await api<BundleApplyOut>(
        `/api/v1/workspaces/${workspaceId}/agents/${agent.id}/bundles/${bundle.id}`,
        { method: "POST", body: bundleApplyBody(bundle, options, false) },
      );
      if (result.needs.length > 0) {
        setError(`Turning on ${bundle.label} failed. Nothing was changed.`);
        return;
      }
      onDone(result);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `Turning on ${bundle.label} failed. Nothing was changed.`);
    } finally {
      setSubmitting(false);
    }
  };

  const lines = reviewLines(bundle, options, connections);
  const rowCount = preview ? preview.grants_created.length + preview.grants_existing.length : 0;

  return (
    <Dialog title={`Turn on ${bundle.label} for ${agent.name}`} open onClose={onClose} wide>
      <div className="space-y-5" data-testid="bundle-setup-dialog">
        <ol className="flex flex-wrap gap-2 text-xs" aria-label="Setup steps">
          {steps.map((name, index) => (
            <li
              key={name}
              aria-current={index === stepIndex ? "step" : undefined}
              className={`rounded-full border px-2.5 py-0.5 ${
                index === stepIndex ? "border-accent bg-accent-soft text-accent-strong" : "border-line text-dim"
              }`}
            >
              {stepLabel(name, connectorNames)}
            </li>
          ))}
        </ol>
        <ErrorNote message={error ?? previewError} />

        {step !== "sandbox" && step !== "repositories" && step !== "review" ? (
          <ConnectionStep
            type={step}
            label={stepLabel(step, connectorNames)}
            connections={activeConnectionsOf(connections, step)}
            value={options.connections[step] ?? ""}
            onChange={(id) => {
              const next = { ...options.connections, [step]: id };
              if (step === "github") delete next.cli;
              update({ connections: next, sandboxMode: sandboxesFor(connections, id).length ? "existing" : "create" });
              const chosen = connections.find((c) => c.id === id);
              if (step === "github" && chosen) {
                update({ sandbox: { ...options.sandbox, name: `Sandbox for ${chosen.name}` } });
              }
            }}
          />
        ) : null}

        {step === "sandbox" ? (
          <section className="space-y-3" data-testid="bundle-step-sandbox">
            <p className="text-sm text-dim">
              Code runs in a CLI Sandbox connection.{" "}
              {existingSandboxes.length === 0 ? "None uses this GitHub connection yet." : ""}
            </p>
            <div className="flex flex-wrap gap-4 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="sandbox-mode"
                  checked={options.sandboxMode === "create"}
                  onChange={() => update({ sandboxMode: "create" })}
                />
                Create one now
              </label>
              {existingSandboxes.length > 0 ? (
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="sandbox-mode"
                    checked={options.sandboxMode === "existing"}
                    onChange={() =>
                      update({
                        sandboxMode: "existing",
                        connections: {
                          ...options.connections,
                          cli: options.connections.cli ?? existingSandboxes[0].id,
                        },
                      })
                    }
                  />
                  Use an existing sandbox
                </label>
              ) : null}
            </div>
            {options.sandboxMode === "existing" ? (
              <Field label="Sandbox">
                <Select
                  value={options.connections.cli ?? ""}
                  onChange={(event) =>
                    update({ connections: { ...options.connections, cli: event.target.value } })
                  }
                >
                  {existingSandboxes.map((sandbox) => (
                    <option key={sandbox.id} value={sandbox.id}>
                      {sandbox.name} · repositories: {allowedRepositoriesOf(sandbox).join(", ") || "none"}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : (
              <>
                <Field label="Name">
                  <Input
                    value={options.sandbox.name}
                    onChange={(event) => update({ sandbox: { ...options.sandbox, name: event.target.value } })}
                  />
                </Field>
                <fieldset className="space-y-2">
                  <legend className="text-[13px] font-medium text-dim">Repositories this sandbox may use</legend>
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="radio"
                      name="sandbox-allowed"
                      checked={options.sandbox.allowedMode === "any"}
                      onChange={() => update({ sandbox: { ...options.sandbox, allowedMode: "any" } })}
                    />
                    <span>
                      Every repository the GitHub connection can reach
                      <span className="block text-xs text-faint">Written as *. GitHub&rsquo;s own token scope still applies.</span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="radio"
                      name="sandbox-allowed"
                      checked={options.sandbox.allowedMode === "list"}
                      onChange={() => update({ sandbox: { ...options.sandbox, allowedMode: "list" } })}
                    />
                    <span>
                      Only these
                      <span className="block text-xs text-faint">One owner/name per line</span>
                    </span>
                  </label>
                  {options.sandbox.allowedMode === "list" ? (
                    <Textarea
                      aria-label="Allowed repositories"
                      rows={3}
                      value={allowedText}
                      onChange={(event) => {
                        setAllowedText(event.target.value);
                        update({ sandbox: { ...options.sandbox, allowed: parseLines(event.target.value) } });
                      }}
                      className="font-mono text-xs"
                    />
                  ) : null}
                  {allowedErrors.length > 0 ? (
                    <p role="alert" className="text-xs text-danger">
                      {allowedErrors[0]}
                    </p>
                  ) : null}
                </fieldset>
              </>
            )}
            <p className="text-xs text-faint">
              Network: none (isolated) · Git credential: {github?.name ?? "GitHub"}
            </p>
          </section>
        ) : null}

        {step === "repositories" ? (
          <section className="space-y-3" data-testid="bundle-step-repositories">
            <p className="text-sm text-dim">Which repositories may this agent use?</p>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="repositories-mode"
                checked={options.repositoriesMode === "any"}
                onChange={() => update({ repositoriesMode: "any" })}
              />
              {bundle.id === "code-editing" ? `Every repository ${sandboxName} allows` : "Every repository"}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="repositories-mode"
                checked={options.repositoriesMode === "list"}
                onChange={() => update({ repositoriesMode: "list" })}
              />
              Only these
            </label>
            {options.repositoriesMode === "list" ? (
              <>
                <Textarea
                  aria-label="Repositories"
                  rows={3}
                  value={repositoriesText}
                  onChange={(event) => {
                    setRepositoriesText(event.target.value);
                    update({ repositories: parseLines(event.target.value) });
                  }}
                  className="font-mono text-xs"
                />
                <ul className="flex flex-wrap gap-1.5 text-xs">
                  {options.repositories.map((entry, index) => (
                    <li key={`${entry}-${index}`}>
                      <Badge tone={repositoryErrors[index] ? "danger" : "neutral"}>{entry}</Badge>
                    </li>
                  ))}
                </ul>
                {repositoryErrors.find(Boolean) ? (
                  <p role="alert" className="text-xs text-danger">
                    {repositoryErrors.find(Boolean)}
                  </p>
                ) : null}
              </>
            ) : null}
            {bundle.tools.some((tool) => "base" in tool.scope) ? (
              <details className="text-sm">
                <summary className={`cursor-pointer text-dim ${focusRing}`}>Advanced: pull request base branch</summary>
                <div className="mt-2">
                  <Field label="Base branch pattern" hint="Pull requests may only target branches matching this.">
                    <Input value={options.base} onChange={(event) => update({ base: event.target.value })} />
                  </Field>
                </div>
              </details>
            ) : null}
          </section>
        ) : null}

        {step === "review" ? (
          <section className="space-y-3" data-testid="bundle-step-review">
            <h3 className="text-sm font-semibold">What {agent.name} will be able to do</h3>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            {bundle.not_included.length > 0 ? (
              <p className="text-xs text-dim">Not included: {bundle.not_included.join(", ")}</p>
            ) : null}
            {bundle.id === "code-editing" && options.sandboxMode === "create" ? (
              <p className="text-xs text-dim">
                Will create: CLI Sandbox connection &lsquo;{options.sandbox.name.trim()}&rsquo; (git credential:{" "}
                {github?.name ?? "GitHub"}; repositories:{" "}
                {options.sandbox.allowedMode === "any" ? "any" : options.sandbox.allowed.join(", ")})
              </p>
            ) : null}
            {previewing ? <Spinner label="Checking what this writes…" /> : null}
            {preview ? (
              <>
                {preview.needs.length > 0 ? (
                  <ErrorNote message={`Still needed: ${preview.needs.map((need) => `${need.kind} ${need.connector_type}`).join(", ")}`} />
                ) : null}
                <details className="text-sm">
                  <summary className={`cursor-pointer text-dim ${focusRing}`}>
                    Show the {rowCount} grants and {preview.rules_added.length} rules this writes
                  </summary>
                  <ul className="mt-2 space-y-1 font-mono text-[12px]" data-testid="bundle-review-rows">
                    {[...preview.grants_created, ...preview.grants_existing].map((row, index) => (
                      <li key={`${row.capability}-${index}`}>
                        {row.capability}{" "}
                        <span className="text-faint">
                          {Object.entries(row.scope_json)
                            .map(([key, value]) => `${key}=${key === "connection_id" && row.connection_name ? row.connection_name : String(value)}`)
                            .join(", ")}
                        </span>
                        {preview.grants_existing.includes(row) ? <span className="text-faint"> · already in place</span> : null}
                      </li>
                    ))}
                    {preview.rules_added.map((rule) => (
                      <li key={`rule-${rule.capability}`}>
                        rule {rule.capability} → {rule.action}
                      </li>
                    ))}
                  </ul>
                </details>
                {preview.warnings.length > 0 ? (
                  <ul className="space-y-1 rounded-xl border border-warn/30 bg-warn-soft px-3 py-2 text-xs text-warn" data-testid="bundle-warnings">
                    {preview.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                ) : null}
                <p className="text-xs text-faint">
                  Callable afterwards: {preview.callable_tools.length} tools
                </p>
              </>
            ) : null}
          </section>
        ) : null}

        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
          <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          {stepIndex > 0 ? (
            <Button type="button" onClick={() => setStepIndex(stepIndex - 1)} disabled={submitting}>
              Back
            </Button>
          ) : null}
          {step === "review" ? (
            <Button
              type="button"
              variant="primary"
              disabled={submitting || previewing || preview === null || preview.needs.length > 0}
              onClick={() => void submit()}
            >
              {submitting ? "Turning on…" : `Turn on ${bundle.label}`}
            </Button>
          ) : (
            <Button type="button" variant="primary" disabled={stepBlocked} onClick={() => setStepIndex(stepIndex + 1)}>
              Next
            </Button>
          )}
        </footer>
      </div>
    </Dialog>
  );
}

function ConnectionStep({
  type,
  label,
  connections,
  value,
  onChange,
}: {
  type: string;
  label: string;
  connections: ConnectionInfo[];
  value: string;
  onChange: (id: string) => void;
}) {
  const chosen = connections.find((c) => c.id === value);
  return (
    <section className="space-y-3" data-testid={`bundle-step-${type}`}>
      <p className="text-sm text-dim">{label} calls go through this connection.</p>
      {connections.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-strong px-4 py-4 text-sm text-dim">
          No active {label} connection. Connect one on the Apps page first.{" "}
          <Link href="/apps" className="text-accent-strong hover:underline">
            Open Apps
          </Link>
        </p>
      ) : (
        <Field label="Connection">
          <Select value={value} onChange={(event) => onChange(event.target.value)}>
            <option value="">Choose a connection…</option>
            {connections.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.name} · {authLabel(connection)}
                {connection.authorized_by ? ` · authorized by ${connection.authorized_by.display_name}` : ""}
              </option>
            ))}
          </Select>
        </Field>
      )}
      {chosen?.authorized_by ? (
        <p className="text-xs text-dim">
          This connection acts with {chosen.authorized_by.display_name}&rsquo;s {label} permissions.
        </p>
      ) : null}
      {type === "github" ? (
        <p className="text-xs text-faint">
          Pushes and pull requests need contents and pull-requests access on the token; a fine-grained
          personal access token is the shortest way to bound that.{" "}
          <a href={TOKEN_DOCS} target="_blank" rel="noreferrer" className="text-accent-strong hover:underline">
            How to set up a token
          </a>
        </p>
      ) : null}
    </section>
  );
}
