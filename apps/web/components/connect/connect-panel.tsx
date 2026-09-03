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
 *   device code                       a code to type, one link away
 *   register an app first             one paste, once per workspace per server
 *   an API key                        the honest last resort
 *
 * The server's answer is the only routing input. It reports which flows can
 * start beside the one it prefers, and the panel renders the preferred flow
 * with the other as a quiet link — so a refusal on one path is never the end
 * of the screen. The API-key form is never removed and never broken; it is
 * reached through a link at the bottom rather than being the first thing
 * anybody sees, and a connector with no credential scheme simply has no
 * such link.
 *
 * Nothing here holds a token. The authorization URL is built by our API and
 * carries only public parameters; the device panel gets a display code and an
 * opaque handle. The browser is never trusted with credential material. */

import { KeyRound, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
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
import {
  connectorSignsIn,
  credentialSchemes,
  describePermissions,
  navigateToProvider,
  postFormTo,
  safeHttpsUrl,
  saveReturnRoute,
} from "@/lib/oauth";
import type {
  ConnectionCreated,
  ConnectionInfo,
  ConnectorInfo,
  OAuthDeviceStartOut,
  OAuthProbeFlow,
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

/** Tolerant of an API that predates the flow fields: absent means "no". */
function flowAvailable(flow?: OAuthProbeFlow): boolean {
  return flow?.available === true;
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
  redirect_flow: { available: false, reason: "" },
  device_flow: { available: false, reason: "" },
  app_settings_url: "",
};

/** GitHub's own limit on an app name, so the field cannot submit an invalid one. */
const GITHUB_APP_NAME_MAX = 34;

const QUIET_LINK = "text-sm text-faint underline underline-offset-2 hover:text-dim";

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
  /**
   * Whether an API key is even a thing here. A connector whose only schemes
   * are sign-ins has no form to fall back to, and offering one would store
   * an empty credential; the panel says so instead.
   */
  const hasCredentialForm = credentialSchemes(connector).length > 0;

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

  /**
   * Probe runs are re-runnable: the first one at mount, another after a
   * client registration is saved, so the server — never this component —
   * decides what a fresh registration can do. A run stays live until the
   * panel unmounts or a newer run starts; a late answer from an older run
   * lands nowhere.
   */
  const mounted = useRef(true);
  const probeRun = useRef(0);
  const probeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      mounted.current = false;
      if (probeTimer.current !== null) clearTimeout(probeTimer.current);
    },
    [],
  );
  const runProbe = useCallback(() => {
    const run = ++probeRun.current;
    const live = () => mounted.current && probeRun.current === run;
    if (probeTimer.current !== null) clearTimeout(probeTimer.current);
    probeTimer.current = setTimeout(() => {
      if (!live()) return;
      setProbeResult(TIMED_OUT_PROBE);
      setState((current) =>
        current.kind === "probing" ? { kind: "choose", probe: TIMED_OUT_PROBE } : current,
      );
    }, PROBE_TIMEOUT_MS);
    const settle = () => {
      if (probeTimer.current !== null) clearTimeout(probeTimer.current);
      probeTimer.current = null;
    };
    probeMutate(
      {
        connector_type: connector.connector_type,
        server_url: serverUrl || undefined,
      },
      {
        onSuccess: (result) => {
          if (!live()) return;
          settle();
          setProbeResult(result);
          setState(nextStateFor(result));
        },
        onError: () => {
          // A probe that fails must never cost somebody the connection: the
          // API-key form is the fallback, exactly as it was before OAuth.
          // Only a connector with no credential form at all has nothing left.
          if (!live()) return;
          settle();
          setState(
            hasCredentialForm
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
  }, [connector.connector_type, hasCredentialForm, probeMutate, serverUrl]);

  useEffect(() => {
    if (!canProbe || probeStarted.current) return;
    probeStarted.current = true;
    runProbe();
  }, [canProbe, runProbe]);

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

  const apiKeyLink = hasCredentialForm ? (
    <button type="button" onClick={showApiKeyLink} className={QUIET_LINK}>
      Use an API key instead
    </button>
  ) : null;

  /** The quiet links under a card, in one row; nothing when there are none. */
  const quietLinks = (...links: (React.ReactNode | null)[]) => {
    const present = links.filter((link) => link !== null);
    if (present.length === 0) return null;
    return (
      <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 border-t border-line pt-3 text-center">
        {present.map((link, index) => (
          <span key={index}>{link}</span>
        ))}
      </div>
    );
  };

  /** The device card's own facts, when that is the state on screen.
   *
   * The card shows when the code is the preferred flow *or* when the person
   * took the "sign in with a code" link from consent — the server said the
   * code can start either way. Anything else in `choose` is the timed-out
   * probe, which has nothing to offer but the key. */
  const chooseProbe = state.kind === "choose" ? state.probe : null;
  const deviceCard =
    chooseProbe !== null &&
    (chooseProbe.method === "device_code" || flowAvailable(chooseProbe.device_flow));
  const redirectAvailable = chooseProbe !== null && flowAvailable(chooseProbe.redirect_flow);
  const secretMissing = chooseProbe?.redirect_flow?.reason === "needs_client_secret";
  const deviceRefused = deviceStart.error !== null && deviceStart.error !== undefined;

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
            accessSummary={
              isGitHubIssuer(state.probe.issuer)
                ? "the permissions the app was registered with"
                : undefined
            }
            installHint={
              isGitHubIssuer(state.probe.issuer)
                ? { url: safeHttpsUrl(state.probe.app_settings_url) }
                : undefined
            }
            onContinue={continueToProvider}
            onUseDeviceCode={
              flowAvailable(state.probe.device_flow)
                ? () => setState({ kind: "choose", probe: state.probe })
                : undefined
            }
            onUseApiKey={hasCredentialForm ? showApiKeyLink : undefined}
            onCancel={onClose}
          />
        ) : null}

        {state.kind === "choose" && deviceCard ? (
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
              {connector.connector_type === "github" ? (
                // A GitHub App starts with the device flow switched off, and GitHub
                // refuses the start until it is on; say so before the click, not
                // after — and say where the working sign-in is when there is one.
                <p className="text-[13px] leading-relaxed text-dim" data-testid="device-github-hint">
                  {redirectAvailable ? (
                    <>
                      GitHub only offers this to apps with{" "}
                      <span className="font-medium text-ink">Enable Device Flow</span> turned on in
                      their settings. Yours may not have it — the browser sign-in needs no change
                      on GitHub.
                    </>
                  ) : (
                    <>
                      GitHub only offers this to apps with{" "}
                      <span className="font-medium text-ink">Enable Device Flow</span> turned on in
                      their settings on GitHub. A GitHub App starts with it off.
                    </>
                  )}
                </p>
              ) : null}
            </div>
            <ErrorNote
              message={errText(deviceStart.error, "That sign-in could not be started.")}
            />
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              {deviceRefused && redirectAvailable ? (
                // The provider said no to the code and the browser sign-in
                // works: that becomes the primary action, the code a retry.
                <>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={beginDeviceFlow}
                    disabled={deviceStart.isPending}
                  >
                    {deviceStart.isPending ? "Starting…" : "Try the code again"}
                  </Button>
                  <Button
                    type="button"
                    variant="primary"
                    data-testid="device-refused-use-redirect"
                    onClick={() => setState({ kind: "consent", probe: state.probe })}
                  >
                    Use the browser sign-in instead
                  </Button>
                </>
              ) : (
                <Button
                  type="button"
                  variant="primary"
                  onClick={beginDeviceFlow}
                  disabled={deviceStart.isPending}
                >
                  {deviceStart.isPending
                    ? "Starting…"
                    : deviceRefused
                      ? "Try the code again"
                      : `Connect ${appName}`}
                </Button>
              )}
            </div>
            {quietLinks(
              redirectAvailable && !deviceRefused ? (
                <button
                  type="button"
                  data-testid="use-redirect-link"
                  onClick={() => setState({ kind: "consent", probe: state.probe })}
                  className={QUIET_LINK}
                >
                  Use the browser sign-in instead
                </button>
              ) : null,
              secretMissing ? (
                <button
                  type="button"
                  data-testid="add-secret-link"
                  onClick={() => setState({ kind: "needs_client", probe: state.probe })}
                  className={QUIET_LINK}
                >
                  Add a client secret to use the browser sign-in instead
                </button>
              ) : null,
              apiKeyLink,
            )}
          </>
        ) : null}

        {state.kind === "choose" && !deviceCard ? (
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
                disabled={!hasCredentialForm}
              >
                Continue with an API key
              </Button>
            </div>
          </>
        ) : null}

        {state.kind === "needs_client" ? (
          <>
            {isGitHubIssuer(state.probe.issuer) && redirectUri.data?.github_app_available ? (
              <GitHubAppCard workspaceId={workspaceId} defaultName="Jhin" />
            ) : null}
            {isGitHubIssuer(state.probe.issuer) &&
            redirectUri.data &&
            !redirectUri.data.github_app_available ? (
              <p
                className="rounded-2xl border border-line bg-raised px-4 py-3 text-[13px] leading-relaxed text-dim"
                data-testid="github-app-by-hand"
              >
                One-click app creation is off on this instance: its address is a loopback or
                plain-HTTP origin that JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS does not list. Create a
                GitHub App on github.com by hand — give it the callback URL below and these
                permissions: {describePermissions(redirectUri.data.github_app_permissions)} — then
                generate a client secret and paste both here. (Or allow-list the origin, restart
                the API, and reopen this dialog.)
              </p>
            ) : null}
            {redirectUri.isPending ? (
              <Spinner label="Loading this instance's redirect URL…" />
            ) : redirectUri.data ? (
              <OAuthClientForm
                workspaceId={workspaceId}
                issuer={state.probe.issuer}
                redirectUri={redirectUri.data.redirect_uri}
                requiresSecret={state.probe.requires_client_secret}
                docsUrl={
                  (isGitHubIssuer(state.probe.issuer)
                    ? safeHttpsUrl(state.probe.app_settings_url)
                    : null) ??
                  connector.docs_url ??
                  undefined
                }
                intro={
                  state.probe.reason === "needs_client_secret"
                    ? `${state.probe.authorization_server_display || state.probe.issuer} needs a client secret for the browser sign-in and none is stored. Paste the client id and secret again — Save replaces the stored pair.`
                    : undefined
                }
                permissions={
                  isGitHubIssuer(state.probe.issuer)
                    ? redirectUri.data.github_app_permissions
                    : undefined
                }
                onSaved={() => {
                  // One registration, one answer: the server says what the
                  // fresh registration can do, rather than this component
                  // deciding it can go straight to consent.
                  setState({ kind: "probing" });
                  runProbe();
                }}
              />
            ) : (
              <ErrorNote message="This instance's redirect URL could not be read, so an app cannot be registered here yet." />
            )}
            {quietLinks(apiKeyLink)}
          </>
        ) : null}

        {state.kind === "device" ? (
          <DeviceCodePanel
            workspaceId={workspaceId}
            device={state.device}
            appSettingsUrl={
              probeResult && isGitHubIssuer(probeResult.issuer)
                ? safeHttpsUrl(probeResult.app_settings_url)
                : null
            }
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
              {hasCredentialForm ? (
                <Button type="button" variant="primary" onClick={showApiKeyLink}>
                  Use an API key instead
                </Button>
              ) : null}
            </div>
          </>
        ) : null}
      </div>
    </Dialog>
  );
}
