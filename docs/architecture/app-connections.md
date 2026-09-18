# App connections

Jhin uses direct provider authentication where the provider supports it. A
self-hosted installation does not need an authentication broker or a public
domain to connect an OAuth-capable MCP server from a browser on the same machine.

## Choose the integration, then authenticate it

The Apps library presents one app with its supported connection methods. Native
adapters retain their own tools and provider-specific authentication. When a
provider offers an official MCP server with browser sign-in, that server is the
direct OAuth route for its MCP tools. For example, Supabase browser sign-in uses
`https://mcp.supabase.com/mcp`; the native Supabase Management API adapter remains
a separate credential and tool contract. A token issued for MCP is never reused
as a Management API token.

Composio is an explicit optional connection method. Configuring its API key does
not replace direct sign-in or migrate existing connections. Managed connections
keep an encrypted account binding locally; Composio owns the provider credential
and its refresh. See [Composio setup](../operations/composio-setup.md).

## Initial checks and agent access

New connections record their initial authentication or provider check in
`last_verified_at`. Saving an API key alone is not a successful check. MCP
connections also discover their tools during setup so a completed sign-in is
ready for assignment. Failed checks retain an actionable connection error.

Connecting an app does not assign it to every agent. Open the connected app in
Apps, choose **Give to an agent**, select the agent and tools, and assign them
there. **Select read-only** selects the tools currently classified as read operations;
**Select all** includes the other available tools. Native tools may additionally
require resource limits such as a project, repository, or schema.

Each assignment uses the existing audited grant API and pins access to that
specific connection. Existing deny rules and approval policies still apply.
If part of an assignment fails, the UI identifies the successful and failed
tools and lets the operator retry the failed portion. The connection's access
summary reports the resulting effective permissions.

## Direct MCP sign-in

Jhin discovers the MCP resource and authorization-server metadata, requires
PKCE with S256, and automatically registers an OAuth client when dynamic client
registration is supported. Registration negotiates the server's advertised
client authentication methods. A provider requiring a registered application
still needs that provider-specific setup; OAuth support alone does not promise
automatic registration.

The browser returns to `/api/v1/oauth/callback` on the configured Jhin origin.
The pending flow binds the workspace, signed-in user, state, PKCE verifier,
redirect URI, issuer, and exact MCP resource. Client secrets and connection
credentials are encrypted locally within the workspace. Refresh runs on the
backend; tokens are not exposed to the browser or model. Discovered tools use
Jhin's existing connection access and tool policy controls.

`APP_URL=http://localhost:3000` supports the normal local browser flow, including
production-like settings. Loopback registrations use the native application
type. External discovery and token endpoints retain their HTTPS and outbound
URL checks. For a remote installation, a private SSH port forward can make the
configured loopback origin reachable from the browser without publishing the
server. The browser must reach the exact registered origin, and the same Jhin
login session must be available there. A reverse proxy deployment can instead
use its configured HTTPS origin.

## Relationship to Hermes and OpenClaw

This follows their documented direct-connection pattern, without claiming
identical implementations:

- Hermes's [official Supabase manifest](https://github.com/NousResearch/hermes-agent/blob/main/optional-mcps/supabase/manifest.yaml)
  selects the vendor's remote MCP URL and OAuth, with discovery, registration,
  PKCE, exchange, and refresh handled by the client.
- Hermes documents browser PKCE as the default, persistent per-profile tokens,
  and automatic refresh. Its [MCP reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference)
  also describes optional device authorization and client metadata documents.
  Its [remote-host guide](https://hermes-agent.nousresearch.com/docs/guides/oauth-over-ssh)
  describes SSH forwarding, callback paste-back, and a desktop callback relay.
- OpenClaw's [MCP CLI](https://docs.openclaw.ai/cli/mcp) performs local browser
  authorization, retains credentials locally, and provides a manual code
  fallback. Its [GitHub skill](https://github.com/openclaw/openclaw/blob/main/skills/github/SKILL.md)
  uses the provider's `gh auth login`. These app flows are separate from its
  [model-provider OAuth](https://docs.openclaw.ai/concepts/oauth).

Jhin does not currently implement client metadata document authentication,
dynamic MCP device authorization, or a manual headless callback paste-back
endpoint. None is required for the current localhost browser default. Adding
one must preserve the same user, workspace, state, issuer, and resource checks.

Catalog precedence is a Jhin product rule, not an inferred authentication
standard: reviewed built-in app identities take precedence over duplicate
community listings. Users still choose the connection method and grant access.
