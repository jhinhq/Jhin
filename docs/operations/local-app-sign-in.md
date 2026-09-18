# Connect apps from a local Jhin installation

Jhin can connect directly to providers that support browser sign-in with a loopback callback. You can run Jhin on your own computer without buying a domain, publishing a website, or configuring Composio. Supabase's hosted MCP server supports automatic client registration: you sign in to Supabase and choose the organization, without creating a personal access token or registering an OAuth app manually. See [Supabase MCP authentication](https://supabase.com/docs/guides/ai-tools/mcp#manual-authentication).

## Jhin on the same computer as your browser

Configure the origin you will actually open, including its port. For the default local web port:

```dotenv
APP_ENV=production
APP_URL=http://localhost:3000
COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
```

Keep `OAUTH_REDIRECT_BASE_URL` unset so the callback follows `APP_URL`. If your deployment already defines an override, remove it or make it the same origin. Production mode permits HTTP on loopback; there is no need to switch to development mode. This exception applies to local access. A public Jhin origin still requires HTTPS and secure cookies.

Apply the environment through your normal deployment procedure, then open `http://localhost:3000` and sign in to Jhin. Changing only the environment does not update an already running API process. The configured web port must serve both Jhin and its `/api` routes; do not use the backend's internal port as the browser origin.

1. Open **Apps**, choose **Supabase**, and use its direct browser sign-in option.
2. Complete sign-in and consent on Supabase's HTTPS page in the same browser. Select the organization containing the projects you intend to use.
3. Return to Jhin, review the discovered tools, and grant the connection to the intended agents.

The direct callback is `http://localhost:3000/api/v1/oauth/callback`. The provider redirects your browser to it; the provider does not need an inbound connection to your computer. Keep the scheme, hostname, and port consistent throughout sign-in. For example, `localhost:3000` and `127.0.0.1:3000` are different browser origins and do not share the same session cookies.

Jhin exchanges and stores the resulting credentials in its encrypted secret store. Provider sign-in and token requests use HTTPS. Signing in connects the app; agents still need Jhin grants and any required tool approvals. This Supabase route uses the hosted MCP server, whose tools differ from Jhin's existing native management and PostgreSQL connectors. Existing connections retain their credentials and grants.

## Jhin on a NAS or another private machine

`localhost` refers to the computer running your browser. To reach a remote Jhin installation through that origin, use a private SSH port forward. For example, if Jhin's web service is reachable on port `3000` on the NAS, run this on your browser computer:

```sh
ssh -N -L 127.0.0.1:3000:127.0.0.1:3000 your-user@your-nas
```

Configure Jhin's `APP_URL` as `http://localhost:3000`, open that exact address on your browser computer, and leave SSH running until sign-in completes. The forward carries the browser's requests and callback to Jhin over SSH. It does not publish a public endpoint or require a public tunnel.

If local port `3000` is occupied, use another local port, for example `-L 127.0.0.1:3300:127.0.0.1:3000`, and set `APP_URL=http://localhost:3300`. The first port is the one the browser and callback use; the final port is the web service on the NAS. Start a fresh sign-in after changing the origin. Opening Jhin through a NAS hostname while its callback points to `localhost` will lose the original browser session.

## What other providers require

Browser sign-in without manual credentials depends on the provider. Providers with dynamic client registration can register Jhin automatically. Others require an operator to register an OAuth app once, or continue to require an API token. Jhin cannot remove those provider requirements.

A supported device authorization flow is another option for a remote installation: the browser enters a short code on the provider's website while Jhin polls for completion, so no callback needs to reach the NAS. GitHub supports this flow, but it still requires a registered app's client ID and device flow enabled in the app's settings. Use it when the connector offers that method; it is not a universal fallback for every app. See [GitHub device authorization](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow).

[Composio sign-in](composio-setup.md) is an optional broker route for deployments that choose it. Its project callback verifier requires public HTTPS, so it is not the route for a private installation that needs to stay on localhost.

## If sign-in does not finish

- **The browser cannot open the callback:** confirm Jhin is running on the exact configured port, or that the SSH forward is still open.
- **Jhin asks you to sign in again or reports an expired request:** start again from the configured `APP_URL` in the same browser. Do not switch between a NAS address, `localhost`, and `127.0.0.1` during the flow.
- **Cookies are missing on return:** use `COOKIE_SECURE=false` for the HTTP loopback origin and `SESSION_COOKIE_SAMESITE=lax`, then sign in to Jhin again after applying the settings.
- **A provider requests app registration or a token:** follow that provider's supported connection method. Automatic registration is available only where the provider supports it.
