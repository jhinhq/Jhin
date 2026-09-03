# Models: providers, profiles, pricing, and balance

Agents never talk to a vendor directly. An admin connects a **provider**
(an endpoint plus an encrypted credential), names **profiles** on it (a model
identifier with prices), and picks a workspace default. The `jhin_models`
package is the only code that knows provider wire formats (plan 15, 47); the
API and workers see `ModelClient`, `ModelRequest`, and `ModelResponse`.

## Components

| Piece | Location | Responsibility |
| --- | --- | --- |
| Adapters | `packages/models/src/jhin_models/providers/` | OpenAI, Anthropic, OpenRouter, Ollama, and generic OpenAI-compatible endpoints. The Ollama adapter also speaks Ollama's native `/api` endpoints for local model management. |
| Factory | `packages/models/src/jhin_models/factory.py` | `build_model_client(type, base_url, api_key, admin_api_key, ...)`; every adapter is wrapped once by the telemetry client. |
| Telemetry | `packages/models/src/jhin_models/telemetry.py` | Payload-free `model.request` spans with `jhin.operation` in `generate`, `stream`, `verify`, `embed`, `list_models`, `account_status`. Ollama's list/show reads report as `list_models`; load and unload report as `model_load`, which the registry folds to `other` (as it does `model_costs`). |
| Price catalog | `packages/models/src/jhin_models/pricing.py` | Static public list prices for OpenAI and Anthropic, `lookup_price`, OpenRouter per-token conversion, the price-source precedence, and the self-hosted rule (`SELF_HOSTED_PROVIDER_TYPES`, `is_self_hosted`, `effective_price`) that every other surface imports rather than restates. |
| Fake provider | `packages/models/src/jhin_models/testing/fake_openai.py` | Dev/test endpoint with deterministic prices, credits, and costs. |
| API | `apps/api/src/jhin_api/models/` | Providers, profiles, verification, model listing, balance, spend, pricing refresh, Ollama load/unload. |
| UI | `apps/web/app/(app)/models/page.tsx`, `apps/web/lib/models.ts`, `apps/web/components/models/ollama-panel.tsx` | Provider cards with a Balance block, profile dialog with auto-filled pricing, Spend tile, Local models panel on the Ollama provider. |

## Data model (migrations `0020`, `0027`, and `0028` for measured pricing)

Migration `0027` (revises `0026`) adds `model_profile.price_source`,
`model_observed_price` (rates measured from real spend, with their evidence),
and `price_catalog_snapshot` (the cached community catalog). `0028` adds
`price_catalog_snapshot.attribution` — split into its own revision because
`0027` had already been applied, and an applied migration must never be
edited in place. See "Where prices come from" below.

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

## Where prices come from

Jhin can learn a model's price from five places, and assumes one more: an
endpoint you host yourself has no per-token price, so a profile on it that
nobody has priced is taken to be free. All six are ranked, and the ranking is
enforced in exactly one place —
`jhin_models.pricing.PRICE_SOURCE_PRECEDENCE`, applied against the database by
`jhin_api.models.pricing_service.apply_best_prices`:

**user-entered > measured from spend > live from the provider > refreshed
catalog > built-in catalog > assumed free (self-hosted) > unknown**

| Rank | `price_source` | Where the number comes from | Trust |
| --- | --- | --- | --- |
| 1 | `user` | An admin typed it (profile create/edit, or the inline fields on an unpriced row). Every price arriving through the public API is stamped this way — nothing automatic writes through that path. | A contract fact. Never overwritten automatically. |
| 2 | `observed` | Measured from the provider's own invoice by `POST /model-profiles/reconcile-pricing` (below). | The rate actually paid, discounts included. |
| 3 | `provider` | The provider's live `/models` response — OpenRouter's `pricing.prompt`/`pricing.completion`, USD per token. | Authoritative list data, straight from the source. |
| 4 | `refreshed_catalog` | The LiteLLM community price map, fetched by `POST /model-profiles/refresh-catalog` and cached. | Community-maintained; fresher than the release but can be stale or wrong. |
| 5 | `catalog` | `jhin_models.pricing`, public list prices as of `CATALOG_UPDATED` (`2026-01`). | Offline floor. Goes out of date between releases. |
| 6 | `self_hosted` | Not a number anyone supplied: `$0 / $0`, because the provider is one you run (`ollama` or `openai_compatible` — `jhin_models.pricing.SELF_HOSTED_PROVIDER_TYPES`) and nothing has been entered. Never written to a row; it is what an unpriced profile on such a provider *resolves to* at read time. | An assumption, and labelled as one: *"Assumed free: a self-hosted endpoint has no per-token price. Enter prices if this endpoint bills you."* Every real source above beats it, and clearing a user price falls back to it rather than to unknown. |
| — | `NULL` | Nothing knows it, and the provider is a hosted vendor. | Reported as **unknown**, never as `$0.00`. |

`price_source` is `NULL` on rows that predate migration `0027` or that were
posted straight to the API. Unknown provenance is treated as *user-entered*:
leaving a stale price alone is a far smaller failure than silently replacing a
real contract price.

### Self-hosted endpoints are assumed free

Ollama and a generic OpenAI-compatible endpoint are, by default, machines you
run: no vendor bills per token, and there is no pricing page to send anyone
to. Treating such a profile as *unknown* would keep every run on it in the
*"n runs aren't included — no price set"* list for as long as the workspace
exists — noise, not honesty. So the zero is assumed, and the assumption is
kept out of the database:

- `is_self_hosted(provider_type)` is the only definition of which types
  qualify. `resolve_price` appends a `self_hosted` `$0` candidate for those
  providers and lets the precedence table do the rest: anything real wins,
  and nothing real leaves `$0`.
- `effective_price(provider_type, input, output, stored_source)` is what a
  reader calls: the stored price with its stored source when both halves are
  set; else a `self_hosted` `$0` candidate when the provider qualifies; else
  `None`. Stored nulls stay null — nothing writes `0` into a row on the
  assumption's behalf — so entering a real price later is an ordinary edit,
  and clearing it returns to *assumed free*, not to *unknown*.
- On the API a self-hosted profile with no stored price counts as priced:
  `GET /model-profiles/pricing-status` reports it at `0` / `0` with
  `price_source: "self_hosted"`, `priced: true`, and the sentence above as its
  label, and it never appears in `untracked` (`untracked_models` loads the
  providers precisely so it can tell). `ModelProfileOut` and the
  pricing-status row carry `assumed_free: true` for exactly this case —
  self-hosted *and* no stored price — so the UI can say so while the edit
  fields stay empty.
- If the endpoint does bill you — a metered proxy, or a hosted service that
  happens to speak the OpenAI wire format — enter its prices. They are stored
  as `user` and outrank the assumption like any other source.

### Honest limitations

- **Neither OpenAI nor Anthropic exposes a pricing endpoint.** There is no API
  that will tell you what a model costs. Everything above rank 3 is either
  measured from a bill or copied from a human-maintained list.
- **Measured rates need an OpenAI admin key.** Only the organization Admin API
  reports what was actually billed. Without one, `reconcile-pricing` skips the
  provider and says why.
- **Organization costs are org-wide.** If another application bills to the same
  OpenAI organization, its dollars land in the numerator while its tokens never
  reach the denominator. See the plausibility guard below.
- **Tracked spend is still an estimate.** It covers only runs Jhin executed,
  and it undercounts anything on an unpriced model — which is why the spend
  tile names those runs instead of implying the total is complete.
- **Self-hosted means assumed free.** An `ollama` or `openai_compatible`
  provider with no entered price is tracked at `$0.00`. That is right for a
  box in the next room and wrong for a metered proxy or a hosted service
  behind the same wire format; the assumption is a floor, and any price you
  enter replaces it.

### Measuring the real rate (`POST /model-profiles/reconcile-pricing`, admin)

`GET /v1/organization/costs?group_by=line_item` itemises spend per invoice
line. Lines look like `"gpt-4o-2024-08-06, input"`; some carry a `quantity` in
tokens. Jhin sums Jhin's own tokens for the same models over the same window
(`agent_run.input_tokens`/`output_tokens` joined to `model_profile.model_name`)
and derives a rate one of four ways, best first
(`jhin_models.observed_pricing`):

| `derivation` | Condition | Arithmetic | Confidence |
| --- | --- | --- | --- |
| `provider_quantity` | The line carries both dollars **and** billed tokens. | `rate = cost / billed_tokens`, per side. Nothing assumed, and immune to the attribution problem — numerator and denominator cover the same traffic. | high |
| `split` | Input and output dollars itemised, no token counts. | `rate_side = cost_side / jhin_tokens_side`. | high |
| `catalog_ratio` | One blended dollar figure for the model. | One equation, two unknowns: `cost = r_in·T_in + r_out·T_out`. Closed with `k = r_out/r_in` from the catalog, because providers discount both sides roughly in step, so the *ratio* survives a contract discount far better than either absolute price. Then `r_in = cost / (T_in + k·T_out)`, `r_out = k·r_in`. **The total is measured; only the split is inferred.** | medium |
| `blended` | One blended figure and no catalog entry to supply a ratio — the case a brand-new model hits. | `r = cost / (T_in + T_out)`, stored as a single blended rate. Never written into the profile's separate input/output columns, because that pair would be invented. | medium |

Guards, all of which report rather than guess:

- **Completed days only.** The window is `[today − 30d, today)` in UTC. Today
  is excluded from *both* halves: its cost buckets are still filling while its
  token counts are already complete, so including it would understate every
  rate.
- **Minimum sample.** At least 3 runs, 10,000 input tokens, 1,000 output
  tokens, and $0.001 billed. Below that, rounding and lumpiness dominate; the
  model is skipped with its actual numbers quoted. (Not applied to
  `provider_quantity`, whose sample is the provider's, not ours.)
- **Plausibility.** A correlated rate more than 3x off the list price drops to
  `confidence: "low"` and is reported but never applied; more than 10x off is
  skipped entirely. The usual cause is other traffic on the same organization.
- **Unattributed spend.** Surface-prefixed lines (`"evals | ..."`) and
  non-model services (`"assistants api | file search"`) are counted into an
  ignored bucket and reported, so the response can say how much of the bill it
  did *not* explain.
- **Cached input.** Jhin's input token count includes cached tokens, so a
  measured input rate is an *effective* rate with cache discounts already in
  it. That is the intent.

Results land in `model_observed_price` (one row per provider + normalised
model key) with the rate, derivation, confidence, the human note, the sample
size, and the period. A `low`-confidence or blended-only rate is stored and
shown but never applied to a profile.

### Refreshing the community catalog (`POST /model-profiles/refresh-catalog`, admin)

Fetches `model_prices_and_context_window.json` from the LiteLLM repository
through the shared public-URL policy (`validate_public_http_url` +
`send_bounded_json`, capped at 8 MB), keeps the OpenAI/Anthropic/OpenRouter
entries that price per token, folds keys to the same normalised spelling the
built-in catalog uses, and stores the ~9 KB projection in
`price_catalog_snapshot`. A failed fetch is reported and falls back to the
previous snapshot, then to the built-in catalog — a refresh failure must never
leave the workspace with no prices at all.

> **Attribution.** The LiteLLM repository is dual-licensed: everything under
> `enterprise/` carries separate terms, and everything else — including this
> root-level data file — is MIT. Caching the map is redistribution, so the
> notice travels with it: **LiteLLM model price map, MIT License, Copyright (c)
> 2023 Berri AI** (<https://github.com/BerriAI/litellm>). The map is fetched at
> run time rather than vendored into this repository, every
> `price_catalog_snapshot` row stores the notice in its `attribution` column,
> and the Models page credits LiteLLM wherever a refreshed-catalog price or the
> refresh action appears. Never fetch anything under `enterprise/`.

### Unknown and stale prices in the UI

A profile with no price does not fail loudly — it records every run at
`$0.00`, which makes the spend total and the budget wrong in the same silent
direction. So:

- `GET /model-profiles/pricing-status` (viewer) returns every profile's price,
  its `price_source` and human label, the measured rate behind it when there is
  one, a `suggestion` when a better source exists, and the models that ran
  unpriced this month.
- Unpriced models get a warning wherever they appear (create dialog, profile
  row, Models page, spend tile) that says spend will not be tracked, links the
  provider's real pricing page, and offers input/output fields inline.
- A self-hosted profile with no stored price is the one case where `$0.00` is
  the truth rather than the failure, so it gets none of that: the profile card
  shows a calm *Free (self-hosted)* badge where a hosted vendor's row would
  say *No price yet*, the dialog's price fields stay empty and editable under
  the note *"Self-hosted endpoints have no per-token price. Enter prices only
  if this endpoint bills you."*, and its runs are counted at `$0` rather than
  listed as untracked. `assumed_free` on the profile is what the UI keys on.
- `catalog_stale` flips once `CATALOG_UPDATED` is more than
  `CATALOG_STALE_AFTER_DAYS` (183) old, and the UI says *"These are list prices
  from {date} — check they're current."*
- `GET /spend` carries `untracked`/`untracked_runs`, so the tile can say *"3
  runs on gpt-5.6-terra aren't included — no price set."*

### Auto-fill in the profile dialog

When a model is picked, prices and context window come from
`GET /model-providers/{id}/models`, whose entries carry
`input_cost_micros_per_million`, `output_cost_micros_per_million`,
`context_window`, and `source` (`provider` live, `catalog` static, or `null`).
On a self-hosted provider (`ollama`, `openai_compatible`) the dialog pre-fills
`0` / `0` instead and says why — the Ollama wording names the host, a generic
OpenAI-compatible endpoint gets the self-hosted note — with the fields left
editable for the endpoint that does bill. Catalog lookup normalises
identifiers: vendor prefixes (`openai/gpt-4o`), dated
snapshots (`gpt-4o-2024-08-06`, `claude-sonnet-4-20250514`,
`claude-opus-4-1@20250805`), and `-latest`/`-preview` suffixes resolve to their
family; longer variants fall back to a dash-delimited prefix
(`gpt-4o-mini-audio` → `gpt-4o-mini`) unless the dropped segment is a bare
version number, so an unknown newer version never inherits an older price.

`POST /model-profiles/{id}/refresh-pricing` (admin) re-runs the whole
precedence chain for one profile and stores the winner. It will not replace a
user-entered price; instead it reports what the better source *would* set, and
`?force=true` takes it — recording the result as the admin's own choice.

## Balance and spend sources

`GET /model-providers/{id}/balance` (viewer) combines Jhin's ledger with a
best-effort call to the provider's billing API:

| Provider | `source` | What the provider reports | Limits |
| --- | --- | --- | --- |
| OpenRouter | `openrouter` | `GET /api/v1/credits`: `total_credits - total_usage` → `provider_remaining_micros`. | Needs the normal API key only. |
| OpenAI | `openai_admin` | Admin API `GET /v1/organization/costs?start_time=<month start>&bucket_width=1d&limit=31` (paginated), summed → `provider_spent_month_micros`. | OpenAI has **no balance API**; this needs a separate *admin key* (OpenAI dashboard → Settings → Organization → Admin keys). Without one the block falls back to tracked spend and offers "Add admin key". |
| Anthropic, Ollama, OpenAI-compatible | `tracked` | Nothing (no billing API through the model endpoint). | Jhin's own numbers only (Ollama shows the local-models panel instead of a balance — see below). |

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

## Ollama: local models

Ollama serves models from a machine you run, so its provider card has
different questions to answer than a hosted vendor's: not how much credit is
left, but which models are pulled, which one is in memory right now, and
whether it can be warmed up before the next run instead of during it. An
Ollama provider therefore shows a **Local models** panel where every other
type shows the Balance block. It lists everything the host has pulled with
size, family, parameter count, quantisation, and context length, marks what
is resident (with VRAM use and when it expires), and gives admins **Load**,
**Unload**, and **Use as model** — the last opens the profile dialog
pre-filled with the model at $0 / $0.

### Routes

All five live under `/model-providers/{id}/ollama/` and are **admin-only**,
like the sibling `/models` route: they decrypt the provider credential and
make a live call to the host. Any other provider type gets a 409, *"This
provider is not an Ollama endpoint; local model management only applies to
providers of type ollama."*, checked before a client is built.

| Route | Scope | Ollama call behind it | Audit action |
| --- | --- | --- | --- |
| `GET …/ollama/models` | `models:read` | `GET /api/tags` for the catalog, `GET /api/ps` for what is resident, then `POST /api/show` per model — the first 24, 5 s each, in parallel — for context length and capabilities. Merged by exact name. | — |
| `GET …/ollama/loaded` | `models:read` | `GET /api/ps`. The panel polls this every 10 s. | — |
| `GET …/ollama/models/{name}` | `models:read` | `POST /api/show {"model": name}`. Names carry `:` and `/` (`tripolskypetr/qwen3.6-uncensored-aggressive:latest`), so the segment is a `{name:path}`. | — |
| `POST …/ollama/load` `{model, keep_alive}` | `models:write` | `POST /api/generate {"model", "keep_alive", "stream": false}` with **no prompt** — Ollama's documented preload. | `provider.ollama_model_loaded` / `provider.ollama_model_load_failed` |
| `POST …/ollama/unload` `{model}` | `models:write` | The same request with `keep_alive: 0`. | `provider.ollama_model_unloaded` / `provider.ollama_model_unload_failed` |

The failure shape follows the rest of this module. Reads never 5xx: an
unreachable or slow host (10 s for `/api/tags` and `/api/ps`) comes back as
an empty list with the redacted reason in `detail`; a failing `/api/ps`
degrades to "nothing loaded" and says so; a failing `/api/show` leaves that
one row with `context_length: null` and no capabilities and never fails the
list. Mutations answer HTTP 200 with `{ok, status, model, keep_alive,
detail}` — the `ProviderVerifyResult` shape — and `detail` is what the panel
renders, including Ollama's own sentence when it refuses (*"ollama: HTTP 404:
model 'nope:latest' not found, try pulling it first"*).

Audit rows carry the provider's display name, the model, the `keep_alive`,
and the outcome (`status: loaded | loading` on success, the redacted
`detail` on failure). Reads write no audit row, and nothing here touches
`model_provider.last_error` — that column belongs to verification.

### `/v1` and the native origin

The provider's `base_url` is the OpenAI-compatible root
(`http://192.168.1.79:11434/v1`), and chat stays on
`{base_url}/chat/completions`. Ollama's management API lives beside that
root, not under it: `/api/tags`, `/api/ps`, `/api/show`, and `/api/generate`
are served from the server origin. The adapter derives the origin once
(`native_origin`: strip trailing slashes, then one trailing `/v1`) and keeps
a second `httpx` client on it — `http://192.168.1.79:11434/v1` becomes
`http://192.168.1.79:11434`, and a base URL without `/v1` is left alone.
`verify` and `list_models` are untouched and still read `/v1/models`.

### `keep_alive`

`keep_alive` is how long Ollama keeps a model resident after its last
request. One validator (`validate_keep_alive`) accepts a duration
(`^[1-9][0-9]*[smh]$`: `5m`, `30m`, `1h`), `-1` for "until unloaded", and
`0` for "unload now". The load route refuses `0` with a 422 (*"keep_alive 0
unloads a model; call unload instead"*): unload is its own route so it gets
its own audit action. The panel offers exactly **5 minutes** (the default,
and Ollama's own `OLLAMA_KEEP_ALIVE` default), **1 hour**, and **Until
unloaded**.

On the wire, durations and `0` go as JSON strings and **`-1` goes as a JSON
number**. Ollama parses string durations with Go's `time.ParseDuration`,
which special-cases `"0"` and accepts `"-1m"` but rejects a unit-less
`"-1"` — `400 {"error": "time: missing unit in duration \"-1\""}` — while
any negative *number* means forever. (Measured against Ollama 0.33.2; the
Ollama FAQ documents the same three forms.)

What Ollama does with it, as observed on 0.33.2:

- The timer restarts on every request that touches the model, and
  `expires_at` in `/api/ps` is re-stamped at each completion. A busy agent
  keeps its model warm without anyone pressing Load.
- A request that carries no `keep_alive` — every chat completion Jhin sends
  — restarts the timer but keeps the duration in force. A model loaded with
  `-1` kept its far-future expiry across chat traffic; it does not fall back
  to five minutes.
- A load request against an already-resident model returns in milliseconds
  with `done_reason: "load"` and replaces the duration without reloading
  anything, so changing your mind from `5m` to `-1` is free.
- `-1` shows in `/api/ps` as an `expires_at` centuries out (Go's
  `math.MaxInt64` nanoseconds — the year 2318 on this host). The API turns
  that into `keeps_loaded: true` (`expires_at` is null or more than a year
  away) so the panel says *stays loaded* instead of a relative time nobody
  wants to read.

### No key needed

Ollama has no credentials: an Ollama provider is saved with `secret_id`
null and every route above works without one. If a key *is* stored it is
sent as `Authorization: Bearer …` on both the `/v1` and the `/api` clients —
useful only when a reverse proxy in front of Ollama asks for one.

### Reaching the host

Ollama listens on `127.0.0.1:11434` by default, which is right for a laptop
and wrong for a server. From the compose stack `localhost` is the API
container itself, so the base URL has to name the machine:
`http://<lan-ip>:11434/v1`, or `http://host.docker.internal:11434/v1` under
Docker Desktop when Ollama runs on the same machine as the stack. The Ollama
host must be started with `OLLAMA_HOST=0.0.0.0` (or an explicit interface)
before anything but itself can connect. The panel's unreachable state names
the base URL it tried for exactly this reason, and Verify on the provider
card is the quickest check that `/v1/models` answers.

### Long loads

Loading a model is the slow operation. On the host this feature was built
against — one GPU, ~18 GB Q4_K_M models (`qwen3.8:latest` 27.3B and
`muse-glimmer:latest` 27.9B) — the first request after a cold start took
55–60 s to answer (`time_total` 54.8 s and 60.8 s), a reload from the host's
page cache 8–13 s, and a resident model answered a one-word prompt in 0.7 s
and a tool-calling reasoning step in one to four seconds. Only one model of
that size fit at a time: loading the second evicted the first, and the poll
shows the swap. An agent that starts on a cold model pays that load inside
its first reasoning step — the chat client's read timeout is 300 s, so the
run succeeds but looks stuck for a minute. Load exists so an admin can pay
it ahead of time.

A cold load can outlast what a proxy allows on one request (Cloudflare
answers 524 at 100 s), so the load route does not wait for it. The adapter
gives `/api/generate` a ten-minute read timeout; the API awaits that call
for at most 20 s (`OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS`) and on timeout
answers `status: "loading"` (*"Ollama is still loading … It will show as
loaded when it finishes."*) while the shielded task finishes in the
background and closes its client. The panel treats `loaded` and `loading`
alike: the row reads *Loading qwen3.8:latest — 17.7 GB, this can take a
minute or more* until the 10 s `/loaded` poll lists the name. Unload is
quick and is awaited in full.

### What the numbers mean

- **Resident size.** `size_bytes` is what `/api/ps` reports as resident and
  `size_vram_bytes` the part of it on the GPU. Equal means the whole model is
  on the GPU; `0` means it runs from system RAM, and the panel says *in RAM*.
- **Two context lengths.** The catalog row's `context_length` comes from
  `/api/show` (`model_info["<architecture>.context_length"]`) and is the
  model's *maximum* — 262 144 for `qwen3.8:latest`. The loaded row's comes
  from `/api/ps` and is the *runtime* context the resident instance was
  started with — 32 768 for the same model. A profile created with **Use as
  model** takes the maximum as its `context_window`; the effective window is
  whatever `num_ctx` Ollama applied.
- **Capabilities** (`completion`, `tools`, `thinking`, `vision`, …) are
  Ollama's word for them; older servers send none, and the row shows none.
- **The listing degrades rather than fails.** A stuck `/api/ps` shows
  everything as not loaded with the reason in `detail`; past 24 models the
  rest are listed without context length or capabilities.
- **No streaming.** The agent runtime calls `generate()`, not `stream()`,
  and the chat view polls for messages, so a slow local model shows nothing
  until the whole reply is in. Ollama's OpenAI endpoint also returns the
  model's chain of thought in a separate `reasoning` field; the adapter
  ignores it, and only the answer reaches the conversation.

### Prices

Ollama has no per-token price and the `/models` listing reports
`source: null` for every local model, so an Ollama provider is treated as
$0 by construction rather than as unknown — the self-hosted rule under
"Where prices come from", which Ollama shares with `openai_compatible`.
Picking a local model in the profile dialog auto-fills `0` / `0` with the
note *"Runs on your Ollama host — no per-token price."*, and **Use as
model** pre-fills the same. Saving those zeros stores a user-entered price
(`price_source: user`); clearing them stores nothing, and the profile then
resolves to `self_hosted` at read time and is badged *Free (self-hosted)*.
Either way a local model's runs are tracked at $0.00 instead of being
reported as unpriced, so they neither inflate the spend total nor appear in
*"n runs aren't included — no price set"* — which is the point. An Ollama
host behind a metered proxy is the exception: enter the proxy's prices and
they win.

## Out-of-credit failures

Adapters classify OpenAI's HTTP 429 with `code == "insufficient_quota"` and
OpenRouter's HTTP 402 as `ModelProviderError(error_code="insufficient_funds")`
with the message *"Your <provider> account is out of credit. Add funds at
<dashboard url>, then retry."* The agent worker raises the Temporal
`ApplicationError` with that type, the workflow records
`error_code="insufficient_funds"` and the friendly `error_message` on the run,
and the conversation's system message carries both; the chat renders an
"Out of credit" card linking to the Models page.

## Reasoning effort and tool calling

OpenAI's reasoning models apply a non-`none` `reasoning_effort` by default,
and `POST /v1/chat/completions` refuses to combine that default with function
tools ("Function tools with reasoning_effort are not supported for
`<model>`… set reasoning_effort to 'none'"). Jhin advertises the agent's
granted tools on essentially every step, so without a fix every reasoning
model is unusable. `jhin_models.reasoning` owns the rule:

- **Reasoning-class detection** is by name shape, not an exact list:
  `o<digit>…` (`o1`, `o3-mini`, `o4-mini`) and `gpt-5` or a later major
  (`gpt-5-mini`, `gpt-5.6-terra`, `gpt-5-2025-08-07`). `gpt-4o`, `gpt-4o-mini`
  and `gpt-4.1` are not; nor is the non-reasoning `gpt-5-chat*` line.
  OpenRouter's `vendor/model:variant` names are normalized first. A profile's
  `supports_reasoning` flag (or `config_json.reasoning.supports_reasoning`)
  forces reasoning-class treatment for a name the matcher misses.
- **Automatic**: on adapters that speak raw chat completions (`openai`,
  `openai_compatible`) a request that carries tools *and* targets a
  reasoning-class model is sent `reasoning_effort: "none"`. Nothing is sent
  otherwise — not on non-reasoning models, not on tools-free requests (there
  is no conflict there, and OpenAI's own default is the better answer).
  OpenRouter normalizes reasoning parameters itself and can route to the
  Responses API, so no workaround is injected there; an explicit setting is
  translated to its native `reasoning` block (`{"enabled": false}` for
  `"none"`). Anthropic and Ollama take no reasoning effort at all.
- **Per-profile override**: `config_json.reasoning = {effort, supports_reasoning}`
  with `effort` one of `none` / `low` / `medium` / `high` (or absent).
  `minimal` is rejected — current reasoning models no longer accept it.
  An explicit effort always wins over the automatic value. Saving one is
  rejected when the provider takes no reasoning effort, or when the model is
  not reasoning-class.
- **The conflict case** (`effort` pinned above `none` *and* the step carries
  tools) is **not** rejected at save time: whether an agent advertises tools
  is a per-agent grant, not a profile fact, so `effort: "high"` is legitimate
  for a tools-free agent. The adapter catches it pre-flight instead and
  raises `model_incompatible_request` naming the model and the pinned effort,
  without spending a round trip.
- **Error mapping**: any 400 whose message or `error.param` mentions
  `reasoning_effort` becomes `error_code="model_incompatible_request"` with a
  plain-language message. The workflow carries that code verbatim (like
  `insufficient_funds`) onto the run and its conversation system message.

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

`GET /v1/organization/costs?group_by=line_item` returns the itemised shape the
real Admin API produces — `"fake-mini, input"` / `"fake-pro, output"` lines
with `quantity`/`quantity_unit`, plus one non-model service line — so the
pricing reconciliation can be exercised end to end without a real
organization. The implied rates are deliberately *not* the list prices above
($0.10 / $0.60 for `fake-mini`, $2.00 / $8.00 for `fake-pro`): discovering a
discount is the point of measuring.
