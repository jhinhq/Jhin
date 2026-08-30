"use client";

/** The single entry point for "Connect".
 *
 * Pressing Connect anywhere in Apps opens this, and this decides how the app
 * is actually connected by asking the server rather than by trusting the
 * catalog's sign-in hint — the index is wrong about that far more often than
 * it is right. The order of preference is fixed and deliberate: a flow where
 * nobody types a secret beats one where somebody does.
 *
 *   discovery + dynamic registration  two clicks, zero fields
 *   a static provider app             two clicks once the app is registered
 *   device code                       a code to type, no redirect URL at all
 *   register an app first             one paste, once per workspace per server
 *   an API key                        the honest last resort
 *
 * The API-key form is never removed and never broken — for the long tail of
 * servers with no OAuth it is the right answer — but it is reached through a
 * quiet link at the bottom rather than being the first thing anybody sees.
 *
 * Nothing here holds a token. The authorization URL is built by our API and
 * carries only public parameters; the device panel gets a display code and an
 * opaque handle. The browser is never trusted with credential material. */

import { KeyRound, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { DeviceCodePanel } from "@/components/connect/device-code-panel";
import { OAuthClientForm } from "@/components/connect/oauth-client-form";
import { OAuthConsentStep } from "@/components/connect/oauth-consent-step";
import {
  CreateConnectionDialog,
  type ConnectionPrefill,
} from "@/components/connection-create-dialog";
import { errText } from "@/components/connection-detail";
import { Button, Dialog, ErrorNote, Field, Input, Spinner } from "@/components/ui";
import {
  useGitHubAppManifest,
  useOAuthDeviceStart,
  useOAuthProbe,
  useOAuthStart,
  useRedirectUri,
} from "@/lib/hooks";
import { connectorSignsIn, navigateToProvider, postFormTo, saveReturnRoute } from "@/lib/oauth";
import type {
  ConnectionCreated,
  ConnectionInfo,
  ConnectorInfo,
  OAuthDeviceStartOut,
  OAuthProbeOut,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export type ConnectState =
  | { kind: "probing" }
  | { kind: "choose"; probe: OAuthProbeOut }
  | { kind: "needs_client"; probe: OAuthProbeOut }
  | { kind: "consent"; probe: OAuthProbeOut }
  | { kind: "redirecting"; url: string }
  | { kind: "device"; device: OAuthDeviceStartOut }
  | { kind: "api_key" }
  | { kind: "failed"; message: string };

/**
 * How long the probe gets before the panel stops waiting on it.
 *
 * A server that has not answered in ten seconds is not going to make this
 * feel like one click, and an API key entered now beats a spinner. The probe
 * is abandoned, not cancelled — a late answer simply lands nowhere.
 */
const PROBE_TIMEOUT_MS = 10_000;

/** GitHub is the one provider with a no-copy-paste setup path, so it is the
 * one issuer this panel recognises by name. */
function isGitHubIssuer(issuer: string): boolean {
  try {
    return new URL(issuer).host.toLowerCase() === "github.com";
  } catch {
    return false;
  }
}

/** Where a probe result sends the panel. Anything not clearly OAuth lands on
 * the API-key form, which is always reachable and always works. */
function nextStateFor(result: OAuthProbeOut): ConnectState {
  if (result.method === "oauth_discovery" || result.method === "oauth_static") {
    return result.client_configured || result.supports_dcr
      ? { kind: "consent", probe: result }
      : { kind: "needs_client", probe: result };
  }
  if (result.method === "oauth_needs_client") return { kind: "needs_client", probe: result };
  if (result.method === "device_code") {
    // Device flow still needs a client id registered for this workspace —
    // `/oauth/device/start` answers 409 without one. Ask for it up front
    // rather than after the person has picked a sign-in method.
    return result.client_configured
      ? { kind: "choose", probe: result }
      : { kind: "needs_client", probe: result };
  }
  return { kind: "api_key" };
}

/** The probe answer the panel invents when the real one never arrives. */
const TIMED_OUT_PROBE: OAuthProbeOut = {
  method: "api_key",
  supports_oauth: false,
  supports_dcr: false,
  issuer: "",
  authorization_server_display: "",
  scopes: [],
  resource: "",
  client_configured: false,
  requires_client_secret: false,
  reason: "timeout",
};

/** GitHub's own limit on an app name, so the field cannot submit an invalid one. */
const GITHUB_APP_NAME_MAX = 34;

/**
 * "Create a GitHub App for this instance" — the path with no copy-paste at
 * all. The browser posts a manifest our API wrote; GitHub creates the app and
 * hands the credentials straight back to the callback. Nobody sees a secret.
 */
function GitHubAppCard({
  workspaceId,
  defaultName,
}: {
  workspaceId: string;
  defaultName: string;
}) {
  const [appName, setAppName] = useState(() => defaultName.slice(0, GITHUB_APP_NAME_MAX));
  const [organization, setOrganization] = useState("");
  const manifest = useGitHubAppManifest(workspaceId);

  return (
    <form
      className="space-y-3 rounded-2xl border border-accent/30 bg-accent-soft px-4 py-3.5"
      data-testid="github-app-manifest-card"
      onSubmit={(event) => {
        event.preventDefault();
        manifest.mutate(
          { app_name: appName.trim(), organization: organization.trim() || null },
          {
            onSuccess: (created) => {
              saveReturnRoute();
              postFormTo(created.post_url, {
                manifest: JSON.stringify(created.manifest),
                state: created.state,
              });
            },
          },
        );
      }}
    >
      <p className="font-display text-sm font-semibold text-ink">
        Create a GitHub App for this instance — one click, no copy-paste
      </p>
      <p className="text-[13px] leading-relaxed text-dim">
        GitHub creates the app and sends its credentials straight back to Jhin. You never see or
        paste a secret.
      </p>
      <Field label="App name" hint="Shown on GitHub. Must be unique across all of GitHub.">
        <Input
          required
          maxLength={GITHUB_APP_NAME_MAX}
          value={appName}
          onChange={(event) => setAppName(event.target.value)}
        />
      </Field>
      <Field label="Organization (optional)" hint="Leave empty to create it on your own account.">
        <Input
          maxLength={100}
          value={organization}
          onChange={(event) => setOrganization(event.target.value)}
          placeholder="my-org"
        />
      </Field>
      <ErrorNote message={errText(manifest.error, "GitHub could not be asked to create the app.")} />
      <Button
        type="submit"
        variant="primary"
        disabled={manifest.isPending || appName.trim() === ""}
      >
        {manifest.isPending ? "Opening GitHub…" : "Create the app on GitHub"}
      </Button>
    </form>
  );
}

export function ConnectPanel({
  workspaceId,
  connector,
  prefill,
  onClose,
  onConnected,
  onCreated,
}: {
  workspaceId: string;
  connector: ConnectorInfo;
  prefill?: ConnectionPrefill;
  onClose: () => void;
  onConnected: (connection: ConnectionInfo) => void;
  /**
   * The API-key path's own result, which carries the one-time webhook secret
   * a connection may come with. Without it that result is narrowed to its
   * connection and handed to `onConnected` — correct, but it would drop a
   * secret that is shown exactly once, so callers with webhooks pass this.
   */
  onCreated?: (created: ConnectionCreated) => void;
}) {
  const { user } = useWorkspace();
  const appName = prefill?.name?.trim() || connector.display_name;
  const serverUrl =
    typeof prefill?.config?.server_url === "string" ? prefill.config.server_url.trim() : "";
  /**
   * Whether there is anything to ask the server about.
   *
   * A connector whose manifest declares no sign-in scheme cannot sign in, and
   * an MCP server with no address yet has nothing to be asked. Both go
   * straight to the credential form with no round trip — which is also why
   * every connector that predates OAuth behaves exactly as it always did.
   */
  const canProbe =
    connectorSignsIn(connector) && (connector.connector_type !== "mcp" || serverUrl !== "");

  const [state, setState] = useState<ConnectState>(() =>
    canProbe ? { kind: "probing" } : { kind: "api_key" },
  );
  /** Kept beside the state machine so the API-key form can still say "this
   * app supports connecting without a key" after the user takes that link. */
  const [probeResult, setProbeResult] = useState<OAuthProbeOut | null>(null);

  const probe = useOAuthProbe(workspaceId);
  const start = useOAuthStart(workspaceId);
  const deviceStart = useOAuthDeviceStart(workspaceId);
  const redirectUri = useRedirectUri();
  const probeStarted = useRef(false);
  const probeMutate = probe.mutate;

  useEffect(() => {
    if (!canProbe || probeStarted.current) return;
    probeStarted.current = true;
    let live = true;
    const timer = setTimeout(() => {
      if (!live) return;
      setProbeResult(TIMED_OUT_PROBE);
      setState((current) =>
        current.kind === "probing" ? { kind: "choose", probe: TIMED_OUT_PROBE } : current,
      );
    }, PROBE_TIMEOUT_MS);
    probeMutate(
      {
        connector_type: connector.connector_type,
        server_url: serverUrl || undefined,
      },
      {
        onSuccess: (result) => {
          if (!live) return;
          clearTimeout(timer);
          setProbeResult(result);
          setState(nextStateFor(result));
        },
        onError: () => {
          // A probe that fails must never cost somebody the connection: the
          // API-key form is the fallback, exactly as it was before OAuth.
          // Only a connector with no credential form at all has nothing left.
          if (!live) return;
          clearTimeout(timer);
          setState(
            connector.auth_schemes.length > 0
              ? { kind: "api_key" }
              : {
                  kind: "failed",
                  message:
                    "Jhin could not work out how to sign in to this app, and it offers no other way to connect.",
                },
          );
        },
      },
    );
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [canProbe, connector.auth_schemes.length, connector.connector_type, probeMutate, serverUrl]);

  const oauthAvailable = probeResult?.supports_oauth === true;
  const showApiKeyLink = () => setState({ kind: "api_key" });
  const backToOAuth = () => {
    if (probeResult && probeResult.method !== "api_key") setState(nextStateFor(probeResult));
    else onClose();
  };

  const continueToProvider = () => {
    start.mutate(
      {
        connector_type: connector.connector_type,
        name: appName,
        config: prefill?.config ?? {},
      },
      {
        onSuccess: (started) => {
          setState({ kind: "redirecting", url: started.authorization_url });
          // Remembered only once we are actually leaving, so a failed start
          // does not strand a return route for some later flow to pick up.
          saveReturnRoute();
          navigateToProvider(started.authorization_url);
        },
      },
    );
  };

  const beginDeviceFlow = () => {
    deviceStart.mutate(
      {
        connector_type: connector.connector_type,
        name: appName,
        config: prefill?.config ?? {},
      },
      { onSuccess: (device) => setState({ kind: "device", device }) },
    );
  };

  // The API-key path is the existing dialog, unchanged, rendered instead of
  // this panel rather than inside it: two stacked modals would be worse than
  // either one.
  if (state.kind === "api_key") {
    return (
      <CreateConnectionDialog
        workspaceId={workspaceId}
        connector={connector}
        prefill={prefill}
        note={
          oauthAvailable
            ? `${appName} supports connecting without a key. An API key you paste here is stored encrypted, but it does not expire and cannot be narrowed to the permissions Jhin needs.`
            : null
        }
        onBack={oauthAvailable ? backToOAuth : undefined}
        onClose={onClose}
        onCreated={(created) => {
          if (onCreated) onCreated(created);
          else onConnected(created.connection);
        }}
      />
    );
  }

  const apiKeyLink =
    connector.auth_schemes.length > 0 ? (
      <div className="border-t border-line pt-3 text-center">
        <button
          type="button"
          onClick={showApiKeyLink}
          className="text-sm text-faint underline underline-offset-2 hover:text-dim"
        >
          Use an API key instead
        </button>
      </div>
    ) : null;

  return (
    <Dialog title={`Connect ${appName}`} open onClose={onClose} wide>
      <div className="space-y-4" data-testid="connect-panel">
        {state.kind === "probing" ? (
          <div className="py-6">
            <Spinner label={`Checking how ${appName} signs in…`} />
          </div>
        ) : null}

        {state.kind === "redirecting" ? (
          <div className="py-6">
            <Spinner label={`Taking you to ${probeResult?.authorization_server_display || "the provider"}…`} />
          </div>
        ) : null}

        {state.kind === "consent" ? (
          <OAuthConsentStep
            appName={appName}
            probe={state.probe}
            userName={user.display_name}
            busy={start.isPending}
            error={errText(start.error, "That connection attempt could not be started.")}
            onContinue={continueToProvider}
            onUseApiKey={connector.auth_schemes.length > 0 ? showApiKeyLink : undefined}
            onCancel={onClose}
          />
        ) : null}

        {state.kind === "choose" && state.probe.method === "device_code" ? (
          <>
            <div className="space-y-3 rounded-2xl border border-accent/30 bg-accent-soft px-4 py-3.5">
              <p className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
                <ShieldCheck size={15} aria-hidden /> Sign in with a code
              </p>
              <p className="text-[13px] leading-relaxed text-dim">
                {appName} will show you a short code to type on its own site. No redirect URL and no
                client secret — this works even when this instance is not reachable from the
                internet.
              </p>
            </div>
            <ErrorNote
              message={errText(deviceStart.error, "That sign-in could not be started.")}
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="primary"
                onClick={beginDeviceFlow}
                disabled={deviceStart.isPending}
              >
                {deviceStart.isPending ? "Starting…" : `Connect ${appName}`}
              </Button>
            </div>
            {apiKeyLink}
          </>
        ) : null}

        {state.kind === "choose" && state.probe.method !== "device_code" ? (
          <>
            <div className="flex items-start gap-3 rounded-2xl border border-line bg-raised px-4 py-3.5">
              <KeyRound size={16} aria-hidden className="mt-0.5 shrink-0 text-dim" />
              <p className="text-[13px] leading-relaxed text-dim">
                Jhin could not work out how {appName} signs in, so the next step is an API key.
              </p>
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="primary"
                onClick={showApiKeyLink}
                disabled={connector.auth_schemes.length === 0}
              >
                Continue with an API key
              </Button>
            </div>
          </>
        ) : null}

        {state.kind === "needs_client" ? (
          <>
            {isGitHubIssuer(state.probe.issuer) ? (
              <GitHubAppCard workspaceId={workspaceId} defaultName="Jhin" />
            ) : null}
            {redirectUri.isPending ? (
              <Spinner label="Loading this instance's redirect URL…" />
            ) : redirectUri.data ? (
              <OAuthClientForm
                workspaceId={workspaceId}
                issuer={state.probe.issuer}
                redirectUri={redirectUri.data.redirect_uri}
                requiresSecret={state.probe.requires_client_secret}
                docsUrl={connector.docs_url || undefined}
                onSaved={() =>
                  setState({
                    kind: "consent",
                    probe: { ...state.probe, client_configured: true },
                  })
                }
              />
            ) : (
              <ErrorNote message="This instance's redirect URL could not be read, so an app cannot be registered here yet." />
            )}
            {apiKeyLink}
          </>
        ) : null}

        {state.kind === "device" ? (
          <DeviceCodePanel
            workspaceId={workspaceId}
            device={state.device}
            onConnected={onConnected}
            onCancel={onClose}
            onRestart={() =>
              setState(probeResult ? nextStateFor(probeResult) : { kind: "api_key" })
            }
          />
        ) : null}

        {state.kind === "failed" ? (
          <>
            <ErrorNote message={state.message} />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button type="button" variant="primary" onClick={showApiKeyLink}>
                Use an API key instead
              </Button>
            </div>
          </>
        ) : null}
      </div>
    </Dialog>
  );
}
