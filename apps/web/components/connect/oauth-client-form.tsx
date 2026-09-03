"use client";

/** "Register Jhin with this server first" — the one-time setup for an
 * authorization server that will not register clients on its own.
 *
 * Most servers never show this: dynamic client registration means Jhin
 * introduces itself and nobody types anything. When it is unavoidable, it is
 * once per workspace per server, and every later connection to that same
 * server skips straight to consent.
 *
 * The redirect URL is the instance's, computed by the server from settings
 * and shown verbatim. It is the value a provider will demand byte-for-byte,
 * so it gets a copy button rather than an invitation to retype it. */

import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { Disclosure } from "@/components/company/bits";
import { CopyRow } from "@/components/connection-detail";
import { Button, ErrorNote, Field, Input, Select } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useCreateOAuthClient } from "@/lib/hooks";
import { describePermissions } from "@/lib/oauth";

type AuthMethod = "none" | "client_secret_post" | "client_secret_basic";

/** The host, for a sentence. Falls back to the raw issuer rather than
 * inventing one — an issuer is an identifier, not always a pretty URL. */
function issuerHost(issuer: string): string {
  try {
    return new URL(issuer).host || issuer;
  } catch {
    return issuer;
  }
}

export function OAuthClientForm({
  workspaceId,
  issuer,
  redirectUri,
  requiresSecret,
  docsUrl,
  intro,
  permissions,
  onSaved,
}: {
  workspaceId: string;
  issuer: string;
  redirectUri: string;
  requiresSecret: boolean;
  docsUrl?: string;
  /** Replaces the "needs Jhin registered first" opener — for the case where
   * a registration exists but is missing the secret the redirect needs. */
  intro?: string;
  /** The permissions the app should be created with, when the provider has
   * a permission model rather than scopes (a GitHub App). */
  permissions?: Record<string, string>;
  onSaved: () => void;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [authMethod, setAuthMethod] = useState<AuthMethod>(
    requiresSecret ? "client_secret_post" : "none",
  );
  const save = useCreateOAuthClient(workspaceId);
  const host = issuerHost(issuer);
  const secretGiven = clientSecret.trim() !== "";

  return (
    <form
      className="space-y-4"
      data-testid="oauth-client-form"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate(
          {
            issuer,
            client_id: clientId.trim(),
            client_secret: secretGiven ? clientSecret : null,
            token_endpoint_auth_method: secretGiven ? authMethod : "none",
          },
          { onSuccess: () => onSaved() },
        );
      }}
    >
      <p
        className="rounded-2xl border border-accent/30 bg-accent-soft px-4 py-3 text-sm leading-relaxed text-ink"
        data-testid="oauth-client-form-intro"
      >
        {intro ?? (
          <>
            <span className="font-medium">{host}</span> needs Jhin registered as an app first. This
            is a one-time setup for your whole workspace — every later connection to this server
            skips it.
          </>
        )}
      </p>

      <CopyRow label="Redirect URL to paste there" value={redirectUri} />

      <ol className="space-y-1 text-[13px] text-dim">
        <li>
          1. Open{" "}
          {docsUrl ? (
            <a
              href={docsUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-accent-strong hover:underline"
            >
              the provider&rsquo;s app settings <ExternalLink size={11} aria-hidden />
            </a>
          ) : (
            <span className="text-ink">the provider&rsquo;s app settings</span>
          )}
          .
        </li>
        <li>2. Create an app and give it the redirect URL above, exactly as shown.</li>
        {permissions && Object.keys(permissions).length > 0 ? (
          <li data-testid="oauth-client-form-permissions">
            2b. Give it these permissions: {describePermissions(permissions)}.
          </li>
        ) : null}
        <li>3. Paste the client ID it gives you back here.</li>
      </ol>

      <Field label="Client ID" hint="Public by design — this one is safe to paste.">
        <Input
          required
          maxLength={500}
          autoComplete="off"
          value={clientId}
          onChange={(event) => setClientId(event.target.value)}
          placeholder="Client ID from the provider"
        />
      </Field>

      {requiresSecret ? (
        <Field
          label="Client secret"
          hint="Stored encrypted (AES-256-GCM envelope); never displayed again."
        >
          <Input
            required
            type="password"
            autoComplete="off"
            maxLength={4096}
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
          />
        </Field>
      ) : (
        <Disclosure
          label="This server also gave me a client secret"
          openLabel="Hide the client secret field"
        >
          <Field
            label="Client secret (optional)"
            hint="Leave this empty when the server accepts a public client — one fewer secret at rest is strictly better."
          >
            <Input
              type="password"
              autoComplete="off"
              maxLength={4096}
              value={clientSecret}
              onChange={(event) => setClientSecret(event.target.value)}
            />
          </Field>
        </Disclosure>
      )}

      {secretGiven ? (
        <Disclosure label="Advanced — how this server expects the secret">
          <Field
            label="Client authentication method"
            hint="Only change this if the provider's docs say it expects HTTP Basic."
          >
            <Select
              value={authMethod}
              onChange={(event) => setAuthMethod(event.target.value as AuthMethod)}
            >
              <option value="client_secret_post">In the request body (most providers)</option>
              <option value="client_secret_basic">HTTP Basic header</option>
            </Select>
          </Field>
        </Disclosure>
      ) : null}

      <ErrorNote
        message={
          save.error
            ? save.error instanceof ApiError
              ? save.error.detail
              : "Saving those app details failed."
            : null
        }
      />

      <div className="flex justify-end">
        <Button type="submit" variant="primary" disabled={save.isPending || clientId.trim() === ""}>
          {save.isPending ? "Saving…" : "Save and continue"}
        </Button>
      </div>
    </form>
  );
}
