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

import { KeyRound, Link2, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
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
            {client.client_secret_configured ? "Secret stored" : "No secret needed"}
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
  /** The GitHub app-manifest callback lands here with a flag. Read once at
   * mount; the effect only scrubs it, so a refresh cannot re-announce a
   * one-off event. */
  const [appCreated] = useState(
    () =>
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("github_app") === "created",
  );

  useEffect(() => {
    if (!appCreated) return;
    window.history.replaceState(null, "", window.location.pathname);
  }, [appCreated]);

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
        {appCreated ? (
          <p
            role="status"
            className="rounded-2xl border border-ok/30 bg-ok-soft px-4 py-3 text-sm text-ok"
          >
            Your GitHub App was created and its credentials were stored. GitHub connections can be
            made from Apps now.
          </p>
        ) : null}

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
                    ? ". Plain HTTP is fine here because it is a loopback address, but most providers will not accept it — this instance is best connected with a sign-in code instead."
                    : ". This is not HTTPS, and most providers will refuse it. Set OAUTH_REDIRECT_BASE_URL to an HTTPS origin this browser can reach."}
              </p>
            </div>
          )}
        </Card>

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
              Nothing registered yet. The first time you connect an app that signs in, one appears
              here on its own.
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
