# Configure optional Composio app sign-in

Jhin prefers direct provider sign-in where available. For a self-hosted installation without a public domain, start with [local app sign-in](local-app-sign-in.md). Supabase's direct browser flow does not require Composio, a personal access token, or manual OAuth app registration.

Composio is an optional broker for supported apps. This guide applies only when you choose that route and can provide its required public HTTPS callback verifier. Jhin retains its own connection IDs, agent permissions, approvals, and audit history. Existing connections and their local credentials are not migrated automatically; a switch to the broker changes credentials only after sign-in succeeds.

## Operator setup

1. Create a Composio project and obtain its project API key. Set `COMPOSIO_API_KEY` in your deployment's protected environment. The Compose manifests pass it only to `api` and `tool-worker`; never put it in browser variables, model prompts, agent configuration, or sandbox environments.
2. Set `APP_URL` to the public HTTPS origin people use to open Jhin, and use secure session cookies with `SameSite=Lax`. A `Strict` session cookie is not sent on the provider's return navigation. Open **Settings → OAuth** to find the managed callback URL. For an installation at `https://jhin.example.com`, it is `https://jhin.example.com/api/v1/oauth/composio/callback`.
3. In the Composio project, open **Settings → General → Configuration** and set the **callback identity verifier URL** to that exact URL. This is mandatory. A per-link return URL is not a substitute for the project verifier.
4. Optionally set `COMPOSIO_AUTH_CONFIGS` to a JSON object mapping lowercase toolkit slugs to existing auth config IDs. Leave it empty or use `{}` when no overrides are needed. Apply database migrations through `0044`, then restart or recreate the API and tool worker with the updated environment using the normal deployment procedure.
5. Sign in to Jhin as a workspace administrator, choose the app in **Apps**, select **Composio · optional hosted service** as its connection method, and complete browser sign-in. Review its tools and grant access to the intended agents. A successful connection does not grant agents access automatically.

The project verifier must be publicly reachable over HTTPS. Composio rejects private and reserved addresses, so `http://localhost:3000` cannot be used as its verifier. A private localhost installation should use the [direct sign-in route](local-app-sign-in.md); that route does not require a public tunnel. For a deployment using Composio, keep the verifier origin and the browser's Jhin origin aligned so the browser returns its session and pending-flow cookies.

If `OAUTH_REDIRECT_BASE_URL` is configured outside the supplied Compose manifests, the URL shown in Settings reflects that override. Use the displayed URL and ensure requests reach the same Jhin deployment. Avoid changing the origin while a sign-in is pending.

Composio defers activation until Jhin redeems a one-use `session_uri` with the signed-in user's workspace-scoped identity. Jhin also requires its short-lived, HttpOnly pending-flow cookie and compares the verified account and toolkit with its stored request. There is no state-only callback or ACTIVE-account polling fallback. Without project callback verification, managed sign-in cannot complete. The verifier prevents someone from sharing their authorization link and attaching another person's provider account to their own Jhin identity. See [Composio callback identity verification](https://docs.composio.dev/reference/api-reference/connected-accounts).

## Auth configs and provider permissions

For supported managed OAuth toolkits without an explicit mapping, Jhin reuses an enabled Composio-managed OAuth2 config or creates one. To choose your own config, set an override such as:

```dotenv
COMPOSIO_AUTH_CONFIGS='{"github":"ac_github_config","vercel":"ac_vercel_config"}'
```

The IDs are not provider credentials. The referenced configs must belong to the same Composio project as the API key, match their toolkit, and be enabled. Native managed connections require OAuth2. Configure each provider's requested permissions to cover the native tools you intend to use; successful browser sign-in alone does not prove every API operation is authorized. For example, check GitHub repository and workflow permissions when enabling repository writes or workflow dispatch. Changes to an auth config's scopes require existing users to reconnect before those new permissions apply. See [Composio scope configuration](https://docs.composio.dev/docs/authentication/controlling-scopes).

**Vercel requires an explicit auth config.** Its toolkit supports OAuth2, but Composio does not supply a managed Vercel OAuth app. Create an OAuth2 config using your Vercel integration credentials in Composio, configure the provider callback as instructed by Composio, and map its ID under `vercel` in `COMPOSIO_AUTH_CONFIGS`. That provider callback is separate from Jhin's mandatory project identity verifier. Jhin's existing Vercel access-token connection remains available. See [Vercel toolkit authentication](https://docs.composio.dev/toolkits/vercel).

## Credentials and tool behavior

Composio holds and refreshes provider tokens. Jhin persists the managed account binding in its encrypted secret store. Native GitHub, Linear, Supabase management, and Vercel tools fetch the needed credential transiently for a call and retain their existing tool schemas and permission checks. Managed native credentials can target only the provider's official API origin. GitHub-backed private repository checkout continues through the native credential path. Supabase PostgreSQL connections still require their dedicated database login.

For apps connected through the generic Composio connector, Jhin stores discovered tool schemas with a concrete toolkit version, executes that pinned version, and applies its existing connection scopes, approvals, and output sanitization. This broker connector is an optional connection method, not the default for every catalog app. New managed tools start at destructive risk; an administrator can review their tool risk settings. Provider annotations cannot silently lower that risk. Update the configured toolkit version, refresh discovery, and review the resulting tools when intentionally upgrading it.

Disabling a connection preserves its grants but stops its use. Reconnecting keeps the local connection identity and leaves a deliberately disabled connection disabled. Deleting a managed connection attempts upstream revocation and account deletion and removes its local credentials and connection-pinned grants.

## Troubleshooting

- **Managed sign-in is unavailable:** supply the project key to both API and tool worker. A key available only to the API can start a connection but cannot execute its tools.
- **Sign-in returns expired or fails after consent:** check the project verifier URL, public HTTPS reachability, and that sign-in began and ended through the same Jhin origin and browser session. Start again from Apps. A copied link, a different account, an expired cookie, or missing workspace-admin membership must not finish the connection.
- **Vercel asks for configuration:** create and map your own OAuth2 config as described above; automatic managed config creation is unavailable for this toolkit.
- **Connected but an operation is refused:** check both the agent's Jhin grants/approval policy and the provider permissions on the auth config. Reconnect when provider permissions change.
- **A provider grant expired or was revoked:** use the connection's reconnect action. Never paste a provider token into public connection settings or reuse a provider callback URL as an app API endpoint.

When checking a deployment, use `docker compose config --quiet` with the same reviewed files and environment used to start it. Full rendered Compose output contains environment values, including the project key; keep it out of logs and support messages.
