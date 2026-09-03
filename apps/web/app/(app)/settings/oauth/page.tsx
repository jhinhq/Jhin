"use client";

/** Settings → OAuth: the two things an operator ever needs to see about
 * sign-in.
 *
 * The redirect URL, because a provider will demand it byte-for-byte and it is
 * derived from this deployment's own settings rather than typed anywhere. And
 * the apps registered against this workspace — most of them registered by
 * Jhin itself, without anybody being asked for anything, which is the whole
 * point of the design.
 *
 * No secret is displayed here, ever. A registration either has one stored or
 * it does not, and that is all this page can say about it. */

import { KeyRound, Link2, LogIn, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { LoadError } from "@/components/company/bits";
import { CopyRow } from "@/components/connection-detail";
import { Badge, Button, Card, ConfirmDialog, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useDeleteOAuthClient, useOAuthClients, useRedirectUri } from "@/lib/hooks";
import type { OAuthClientOut } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const SOURCE_LABELS: Record<OAuthClientOut["source"], string> = {
  dcr: "Registered automatically",
  manual: "Set up by an admin",
  static: "Configured for this instance",
};

/** The host, for a heading. An issuer is an identifier and not always a
 * pretty URL, so an unparseable one is shown exactly as it is stored. */
function issuerHost(issuer: string): string {
  try {
    return new URL(issuer).host || issuer;
  } catch {
    return issuer;
  }
}

/** What the secret badge says. A GitHub registration without a secret can
 * only sign in with a code — the browser sign-in needs the secret — so the
 * badge says that rather than the generic "no secret needed". */
function secretLabel(client: OAuthClientOut): string {
  if (client.client_secret_configured) return "Secret stored";
  if (issuerHost(client.issuer) === "github.com") return "No secret — sign-in code only";
  return "No secret needed";
}

const SIGN_IN_ORDER: Record<"redirect" | "device_code", string> = {
  redirect:
    "In the browser first. Connect GitHub sends the person to github.com and back to the redirect URL above; no setting on GitHub is needed. A sign-in code is offered as an alternative, and when GitHub refuses the code for an app, the browser sign-in is offered in return.",
  device_code:
    "With a sign-in code first, set by OAUTH_PREFER_DEVICE_CODE. The browser sign-in stays available as an alternative. A GitHub App created from Jhin starts with device sign-in turned off; to use codes with it, turn on Enable Device Flow in the app's settings on GitHub.",
};

function ClientRow({
  client,
  canManage,
  onForget,
}: {
  client: OAuthClientOut;
  canManage: boolean;
  onForget: (client: OAuthClientOut) => void;
}) {
  return (
    <li
      data-testid={`oauth-client-${client.id}`}
      className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-line bg-raised px-3.5 py-3"
    >
      <div className="min-w-0 space-y-1">
        <p className="truncate text-sm font-medium text-ink">{issuerHost(client.issuer)}</p>
        <p className="truncate font-mono text-xs text-faint">{client.client_id}</p>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={client.source === "dcr" ? "accent" : "neutral"}>
            {SOURCE_LABELS[client.source]}
          </Badge>
          <Badge tone="neutral">
            <KeyRound size={11} aria-hidden />
            {secretLabel(client)}
          </Badge>
          <span className="text-xs text-faint">
            {client.connection_count === 1
              ? "1 connection"
              : `${client.connection_count} connections`}{" "}
            · added {formatDateTime(client.created_at)}
          </span>
        </div>
      </div>
      {canManage ? (
        <Button size="sm" variant="danger" onClick={() => onForget(client)}>
          Forget
        </Button>
      ) : null}
    </li>
  );
}

export default function OAuthSettingsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const redirect = useRedirectUri();
  const clients = useOAuthClients(workspaceId, isAdmin);
  const forget = useDeleteOAuthClient(workspaceId);
  const [pendingForget, setPendingForget] = useState<OAuthClientOut | null>(null);

  if (!isAdmin) {
    return (
      <>
        <PageHeader title="OAuth" description="How this workspace signs in to other apps." />
        <PageBody className="max-w-3xl">
          <EmptyState
            title="OAuth setup is managed by admins"
            description="Ask a workspace admin if an app needs connecting or reconnecting."
          />
        </PageBody>
      </>
    );
  }

  const clientList = clients.data ?? [];

  return (
    <>
      <PageHeader
        title="OAuth"
        description="The callback URL this instance uses, and the apps it has registered."
      />
      <PageBody className="max-w-3xl space-y-8">
        <Card as="section">
          <h2 className="mb-1 flex items-center gap-2 font-display text-base font-semibold">
            <Link2 size={16} aria-hidden /> Redirect URL
          </h2>
          <p className="mb-4 text-sm text-dim">
            Providers redirect the browser back here after somebody approves a connection. Paste it
            exactly as shown — a provider compares it character for character, and most reject a
            trailing slash you did not mean to add.
          </p>
          {redirect.isPending ? (
            <Spinner label="Reading this instance's redirect URL…" />
          ) : redirect.isError || !redirect.data ? (
            <LoadError what="this instance's redirect URL" onRetry={() => void redirect.refetch()} />
          ) : (
            <div className="space-y-3">
              <CopyRow label="Redirect URL" value={redirect.data.redirect_uri} />
              <CopyRow label="GitHub App callback" value={redirect.data.github_app_redirect_uri} />
              <p className="text-xs text-faint">
                Derived from{" "}
                <code className="font-mono">{redirect.data.configured_via}</code>
                {redirect.data.is_https
                  ? "."
                  : redirect.data.is_loopback
                    ? ". Plain HTTP is fine here because it is a loopback address. Providers that redirect the browser (GitHub does) can still send it back to this machine; one-click GitHub App creation needs this origin listed in JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS."
                    : ". This is not HTTPS, and most providers will refuse it. Set OAUTH_REDIRECT_BASE_URL to an HTTPS origin this browser can reach."}
              </p>
            </div>
          )}
        </Card>

        {redirect.data ? (
          <Card as="section" data-testid="github-sign-in-order">
            <h2 className="mb-1 flex items-center gap-2 font-display text-base font-semibold">
              <LogIn size={16} aria-hidden /> How GitHub signs in
            </h2>
            <p className="text-sm text-dim">{SIGN_IN_ORDER[redirect.data.preferred_sign_in]}</p>
          </Card>
        ) : null}

        <Card as="section">
          <h2 className="mb-1 flex items-center gap-2 font-display text-base font-semibold">
            <ShieldCheck size={16} aria-hidden /> Registered apps
          </h2>
          <p className="mb-4 text-sm text-dim">
            One per authorization server, shared by every connection this workspace makes to it.
            Most are registered by Jhin automatically, with nobody asked for anything.
          </p>
          {clients.isPending ? (
            <Spinner label="Loading registered apps…" />
          ) : clients.isError ? (
            <LoadError what="the registered apps" onRetry={() => void clients.refetch()} />
          ) : clientList.length === 0 ? (
            <p className="rounded-2xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
              Nothing registered yet. Connect GitHub from Apps, or any app that signs in, and one
              appears here on its own.
            </p>
          ) : (
            <ul className="space-y-2">
              {clientList.map((client) => (
                <ClientRow
                  key={client.id}
                  client={client}
                  canManage={isAdmin}
                  onForget={setPendingForget}
                />
              ))}
            </ul>
          )}
          <ErrorNote
            message={
              forget.error
                ? forget.error instanceof ApiError
                  ? forget.error.detail
                  : "Forgetting that registration failed."
                : null
            }
          />
        </Card>
      </PageBody>

      <ConfirmDialog
        open={pendingForget !== null}
        title="Forget this registration?"
        body={
          pendingForget ? (
            <>
              Jhin will forget its app at{" "}
              <strong className="text-ink">{issuerHost(pendingForget.issuer)}</strong> and the
              secret stored with it.{" "}
              {pendingForget.connection_count > 0
                ? `${pendingForget.connection_count === 1 ? "One connection" : `${pendingForget.connection_count} connections`} using it will need reconnecting.`
                : "No connection is using it."}{" "}
              A server that registers clients automatically will simply register a new one next
              time.
            </>
          ) : null
        }
        confirmLabel="Forget it"
        busy={forget.isPending}
        onClose={() => setPendingForget(null)}
        onConfirm={() => {
          if (pendingForget === null) return;
          forget.mutate(pendingForget.id, { onSettled: () => setPendingForget(null) });
        }}
      />
    </>
  );
}
