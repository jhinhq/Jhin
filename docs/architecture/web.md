# Web access

Agents get internet access two ways, both opt-in and deny-by-default. Neither
exists until an admin explicitly sets it up, and everything that comes back
from the web is treated as untrusted external content.

| Path | What runs the search | Authorization | Where results appear |
| --- | --- | --- | --- |
| 1. `web` connector | Jhin's tool worker calls a search API / fetches a page | Tool grants (`web.search`, `web.fetch`) per agent, scoped per connection | `tool_call` rows, sanitized outputs in the transcript |
| 2. Model-native search | The model provider, inside the chat completion | Per-model-profile flag `config_json.web_search` | The reply text, with a labeled "Sources" block |

## Path 1: the `web` connector

`packages/connectors/src/jhin_connectors/web/` is a normal connector
(docs/architecture/connectors.md): manifest-driven UI, encrypted credential,
tools registered into the shared catalog and executed only by the tool
worker behind the gateway.

**Connection.** Auth scheme `bearer` stores one search API key and pins a
`search_backend` — `tavily`, `brave`, or `exa`, each spoken in its real wire
shape (Tavily `POST /search` with a bearer token, Brave
`GET /res/v1/web/search` with `X-Subscription-Token`, Exa `POST /search`
with `x-api-key`). Auth scheme `none` creates a fetch-only connection.
Public config: an optional `base_url` override (policy-checked; how the dev
fake is reached) and optional `allowed_domains` glob patterns.

**`web.search`** (capability `web.search`, read risk). Input
`{connection_id, query, max_results ≤ 10}`. The backend response is
normalized to a bounded list of `{title, url, snippet, published?}` and
labeled untrusted. Grants scope on `connection_id`.

**`web.fetch`** (capability `web.fetch`, read risk). Input
`{connection_id, url}`. Policy, in order:

1. `validate_public_http_url` (shared with the MCP/HTTP connectors): public
   `https` only; anything else needs an exact operator allow-list entry in
   `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`;
2. the connection's `allowed_domains` patterns, when set;
3. the grant's optional `domain` scope — the scope value is always derived
   from the URL's host in the input model, so a grant like
   `domain=*.python.org` cannot be bypassed by a forged field.

The fetch is GET-only, bounded by a timeout and a byte budget, and never
follows a redirect across origins (a few same-origin hops are allowed).
HTML is reduced to readable text with a stdlib `html.parser` extractor
(scripts/styles/noscript dropped, ≤ 20k chars); binary responses are
rejected outright. Output carries the untrusted-content notice.

**Verification.** Bearer connections make one cheap 1-result search;
fetch-only connections just re-run the policy checks.

**Apps library.** `catalog.json` has "Web search (Tavily/Brave/Exa)" entries
pointing at the web connector with the backend pre-selected via the new
`connector_config` prefill, plus a dev-only "Fake web search" entry.

**Wizard preset.** "Web search & browsing" grants `web.search` +
`web.fetch` (domain `*`) pinned to the workspace's web connection.

**Dev fake.** `jhin_connectors.testing.fake_websearch` (compose service
`fake-websearch`, host port 8097) speaks all three backend shapes
deterministically and serves fetchable pages (including a huge page, a
binary, a same-origin redirect, and a cross-origin redirect) so the whole
path runs with zero real credentials. Its origin is allow-listed in
`compose.dev.yaml` exactly like `fake-mcp`.

## Path 2: model-native web search

Some providers can search the web *inside* the model call: OpenAI's
`web_search_options` on chat completions (search-preview models only),
OpenRouter's `web` plugin, and Anthropic's server-side
`web_search_20250305` tool on `/v1/messages`. No Jhin tool effect exists —
nothing passes the gateway — so this is a per-profile opt-in, not a grant:

- `model_profile.config_json.web_search = {enabled, max_uses?}`
  (`jhin_models.web_search.WebSearchConfig`), edited via the "Model's
  built-in web search" toggle on the profile dialog;
- profile create/update rejects the flag with a clear message when the
  provider/model cannot honor it (`web_search_unsupported_reason`); the
  adapters also fail loudly rather than silently dropping a stale flag;
- the agent snapshot carries the config and the reasoning step passes it
  through `ModelRequest.web_search`; adapters translate it to their wire
  format;
- provider citations (`url_citation` annotations, Anthropic
  `web_search_result_location` citations) are appended to the reply text as
  a visible `Sources (provider web search):` block.

Ollama and generic OpenAI-compatible endpoints have no model-native search;
the flag is rejected at validation for them.

## Risks

- **Prompt injection.** Search snippets and fetched pages are
  attacker-controllable text. Outputs are bounded, labeled untrusted, and
  never interpreted as instructions by the platform; agents still read
  them, so grant web access deliberately and scope fetch domains where the
  use case allows.
- **SSRF.** The shared URL policy refuses private/link-local/localhost
  targets unless the operator explicitly allow-lists an origin; redirects
  cannot escape the validated origin.
- **Data volume.** Every projection has hard caps (10 results, 20k chars,
  256 KiB reads) so a hostile page cannot flood transcripts or budgets.
- **Spend.** Model-native search bills on the provider account; `max_uses`
  bounds it where the provider supports a cap.
