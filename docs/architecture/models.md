# Models: providers, profiles, pricing, and balance

Agents never talk to a vendor directly. An admin connects a **provider**
(an endpoint plus an encrypted credential), names **profiles** on it (a model
identifier with prices), and picks a workspace default. The `jhin_models`
package is the only code that knows provider wire formats (plan 15, 47); the
API and workers see `ModelClient`, `ModelRequest`, and `ModelResponse`.

## Components

| Piece | Location | Responsibility |
| --- | --- | --- |
| Adapters | `packages/models/src/jhin_models/providers/` | OpenAI, Anthropic, OpenRouter, Ollama, and generic OpenAI-compatible endpoints. |
| Factory | `packages/models/src/jhin_models/factory.py` | `build_model_client(type, base_url, api_key, admin_api_key, ...)`; every adapter is wrapped once by the telemetry client. |
| Telemetry | `packages/models/src/jhin_models/telemetry.py` | Payload-free `model.request` spans with `jhin.operation` in `generate`, `stream`, `verify`, `embed`, `list_models`, `account_status`. |
| Price catalog | `packages/models/src/jhin_models/pricing.py` | Static public list prices for OpenAI and Anthropic, `lookup_price`, OpenRouter per-token conversion. |
| Fake provider | `packages/models/src/jhin_models/testing/fake_openai.py` | Dev/test endpoint with deterministic prices, credits, and costs. |
| API | `apps/api/src/jhin_api/models/` | Providers, profiles, verification, model listing, balance, spend, pricing refresh. |
| UI | `apps/web/app/(app)/models/page.tsx`, `apps/web/lib/models.ts` | Provider cards with a Balance block, profile dialog with auto-filled pricing, Spend tile. |

## Data model (migration `0020`, revises `0019`)

- `model_provider`: `type`, `display_name`, `base_url`, `secret_id` (API key),
  **`admin_secret_id`** (optional billing credential, `ON DELETE SET NULL`),
  **`credits_loaded_micros`** (prepaid credit the admin entered), `enabled`,
  `last_verified_at`, `last_error`.
- `model_profile`: `provider_id`, `model_name`, `display_name`,
  `context_window`, `input_cost_micros_per_million`,
  `output_cost_micros_per_million`, capability flags, `config_json`.
- `agent_run.estimated_cost_micros` is the spend ledger: the agent worker
  multiplies token usage by the profile's prices on every step.
- `workspace.settings_json.budget`: `{monthly_budget_micros, warning_threshold}`
  (validated by `PATCH /workspaces/{id}` alongside `delegation` and
  `concurrency`).

All money is stored as integer micro-dollars (`$1 = 1_000_000`); prices are
micro-dollars per million tokens.

## Credentials

API keys and admin keys go through the secret store exactly the same way:
the UI stores the value as a secret (`POST /secrets`), the provider row only
references its id, and the value is decrypted in process memory for one call
and discarded. Neither key is ever returned by the API; `ModelProviderOut`
exposes `has_admin_key: bool` only.

## Pricing sources

When a model is picked in the profile dialog the prices (and context window)
are auto-filled from `GET /model-providers/{id}/models`, whose entries carry
`input_cost_micros_per_million`, `output_cost_micros_per_million`,
`context_window`, and `source`:

| Provider | `source` | Where the price comes from |
| --- | --- | --- |
| OpenRouter | `provider` | Its `/models` response: `pricing.prompt` / `pricing.completion` are USD per token; micros per 1M tokens = price × 1,000,000 tokens × 1,000,000 micros. `-1` (dynamic) yields no price. |
| OpenAI, Anthropic | `catalog` | `pricing.py`, the public list prices as of `CATALOG_UPDATED` (`2026-01`). The UI says so and asks the admin to edit when their contract differs. |
| OpenAI-compatible, Ollama | `provider` or none | Used when the endpoint's `/models` carries OpenRouter-style `pricing`; otherwise unknown and the dialog asks for manual prices. |

Catalog lookup normalises identifiers: vendor prefixes (`openai/gpt-4o`),
dated snapshots (`gpt-4o-2024-08-06`, `claude-sonnet-4-20250514`,
`claude-opus-4-1@20250805`), and `-latest`/`-preview` suffixes resolve to
their family; longer variants fall back to a dash-delimited prefix
(`gpt-4o-mini-audio` → `gpt-4o-mini`) unless the dropped segment is a bare
version number, so an unknown newer version never inherits an older price.
Unknown models return `null` prices.

`POST /model-profiles/{id}/refresh-pricing` (admin) repeats the lookup for an
existing profile — provider list first, catalog second — and stores the
result; the profile row's "Refresh prices" action calls it.

Profile create/update still accept explicit micros; auto-fill is a UI
convenience, never a server-side override.

## Balance and spend sources

`GET /model-providers/{id}/balance` (viewer) combines Jhin's ledger with a
best-effort call to the provider's billing API:

| Provider | `source` | What the provider reports | Limits |
| --- | --- | --- | --- |
| OpenRouter | `openrouter` | `GET /api/v1/credits`: `total_credits - total_usage` → `provider_remaining_micros`. | Needs the normal API key only. |
| OpenAI | `openai_admin` | Admin API `GET /v1/organization/costs?start_time=<month start>&bucket_width=1d&limit=31` (paginated), summed → `provider_spent_month_micros`. | OpenAI has **no balance API**; this needs a separate *admin key* (OpenAI dashboard → Settings → Organization → Admin keys). Without one the block falls back to tracked spend and offers "Add admin key". |
| Anthropic, Ollama, OpenAI-compatible | `tracked` | Nothing (no billing API through the model endpoint). | Jhin's own numbers only. |

Fields:

- `tracked_spent_month_micros` / `tracked_spent_total_micros`: sums of
  `agent_run.estimated_cost_micros` for runs whose profile belongs to the
  provider (calendar month in UTC / all time).
- `credits_loaded_micros`: the admin-entered prepaid amount.
- `estimated_remaining_micros`: `credits - provider month spend` when both are
  known, else `credits - tracked total` when credits are set, else `null`.
- `detail`: the human note ("Live from OpenRouter", "From OpenAI's admin API
  (month to date)", or why the live lookup was skipped).

Provider calls use a short timeout and are cached in the API process per
provider id for 60 seconds (`ACCOUNT_STATUS_CACHE_TTL_SECONDS`) — the UI polls
once a minute — and any failure degrades to `source: "tracked"` with the
redacted reason in `detail`. Changing a provider's credentials clears its
cache entry.

`GET /workspaces/{id}/spend` returns the workspace's tracked spend this month
and all time, a per-provider breakdown, and the optional budget; the Models
page header tile and Settings → "Model spend and budget" render it.

Budgets are enforced (plan 15.5): the agent worker refuses to admit a new run
— and stops an in-flight run before its next reasoning step — once the
month's tracked spend meets the agent's `monthly_budget_cents` or the
workspace's `monthly_budget_micros` (`jhin_db.budget`, error code
`budget_exceeded`). Crossing `warning_threshold` (default 0.8) surfaces a
budget notice on the Attention page and a `budget.warning` log event.

Tracked spend is an *estimate*: it only covers runs Jhin executed and uses
the profile's configured prices, so it undercounts anything billed outside
Jhin (other apps on the same key, image generation, embeddings without a
priced profile) and drifts when prices are stale.

## Out-of-credit failures

Adapters classify OpenAI's HTTP 429 with `code == "insufficient_quota"` and
OpenRouter's HTTP 402 as `ModelProviderError(error_code="insufficient_funds")`
with the message *"Your <provider> account is out of credit. Add funds at
<dashboard url>, then retry."* The agent worker raises the Temporal
`ApplicationError` with that type, the workflow records
`error_code="insufficient_funds"` and the friendly `error_message` on the run,
and the conversation's system message carries both; the chat renders an
"Out of credit" card linking to Advanced → Models.

## Model-native web search

`config_json.web_search = {enabled, max_uses?}` on a model profile opts the
profile into the provider's own web search inside the chat completion
(OpenAI search-preview models via `web_search_options`, OpenRouter's `web`
plugin, Anthropic's server-side `web_search_20250305` tool). Validation
rejects the flag on providers/models that cannot honor it, the reasoning
path passes it through `ModelRequest.web_search`, and provider citations
are appended to the reply as a visible "Sources" block. Details and risks:
[web.md](web.md).

## Fake provider (dev and tests)

`python -m jhin_models.testing.fake_openai` (the `fake-provider` compose
service) serves `GET /v1/models` with OpenRouter-style pricing for
`fake-mini` ($0.15 / $0.60 per 1M, 128k context) and `fake-pro`
($2.50 / $10, 200k), `GET /v1/credits` (`total_credits 50`, `total_usage
12.5`), `GET /v1/organization/costs` (three daily buckets of $1.25), and a
`no-credit` model that answers like OpenAI's out-of-quota response. A
provider of type `openai_compatible` with base URL
`http://fake-provider:8080/v1` therefore shows auto-filled prices; an
`openrouter` or `openai` provider pointed at the same base URL shows the
fake balance and month-to-date spend.
