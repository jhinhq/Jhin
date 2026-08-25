# API versioning and compatibility

Every route Jhin serves lives under `/api/v1`. This document says what that
`v1` promises, what would break it, and what stops a breaking change from
shipping by accident.

Companion documents: [API keys](api-keys.md) for the credential and its
scopes, [Roles and permissions](rbac.md) for the ceiling every key sits under.

## What you are integrating against

* **Base URL** — `/api/v1` on your own install. There is no hosted service.
* **The reference** — generated from the running app, not written by hand.
  Signed-in users read it at `/api-docs`; the document itself is
  `GET /api/v1/openapi.json` (session) and, in development only,
  `/openapi.json`, `/docs`, and `/redoc`.
* **Version discovery** — `GET /api/v1/health` needs no credential and
  returns:

  ```json
  { "status": "ok", "app": "Jhin", "version": "0.1.0", "api_version": "v1" }
  ```

  `version` is the release of the install; `api_version` is the contract. Read
  `version` when you need to know whether a feature exists yet, and
  `api_version` when you need to know which contract you are speaking.
* **The snapshot** — [`docs/api/openapi.v1.json`](../api/openapi.v1.json) is
  the published shape of `v1` as of the current commit. It is checked into the
  repository so that a change to the API is visible as a diff in review, and
  so the compatibility test has something to compare against.

## The promise

> Within `/api/v1`, anything that worked keeps working. New capability arrives
> beside the old shape, never in place of it.

Concretely, an integration may rely on all of the following:

* An endpoint that exists will keep existing, at the same method and path.
* A field you send will keep being accepted, with the same type and the same
  meaning.
* A field you read will keep being present, with the same type. A field
  documented as always present stays always present.
* A request you are allowed to make today, with the scopes you hold today,
  stays allowed. The scope an endpoint requires does not change under you, and
  an endpoint that needs no credential does not start needing one.
* A status code the endpoint documents stays documented.
* `operationId` — the name generated SDKs give each method — is stable.

## Backwards-compatible changes

These ship in any release, at any time, with no announcement beyond the
changelog. Update the snapshot and move on.

| Change | Why it is safe |
|---|---|
| A new endpoint | Nobody is calling it yet |
| A new **optional** request field | Requests that omit it behave as before |
| A new response field | A reader that does not know about it ignores it |
| A new accepted value for a request enum | Every value you already send still works |
| A new value a response enum may return | Only if you are not matching exhaustively — see below |
| Widening a type (`string` → `string \| null`, adding a union arm) | Every old value is still valid |
| Relaxing validation (a longer maximum, a looser pattern) | Everything that passed still passes |
| A new documented status code | You were already handling unknown statuses |
| Making a required request field optional | Requests that send it are unaffected |
| A new optional query parameter | Requests that omit it behave as before |
| Marking an endpoint or field deprecated | Nothing stops working |

**About new enum values.** A response enum may gain values. Treat unknown
values as "something new I do not handle yet" rather than as an error — a
`switch` with a `default`, not one that throws. Where a field is genuinely
closed (a `status` that is only ever `ok`), the document says so and it stays
that way.

## Breaking changes

These may not ship inside `/api/v1`. Each of them is detected by the test
described below.

* Removing an endpoint, or moving it to a different path or method.
* Removing or renaming a request field, a response field, or a parameter.
* Changing the type of any field (including narrowing `string | null` back to
  `string`).
* Adding a **required** request field or parameter, or making an existing
  optional one required.
* A response field that was always present becoming conditional.
* Removing a value from an enum, or closing a previously open field into one.
* Tightening validation: a shorter maximum, a stricter pattern, a narrower
  range.
* Changing a default, or the status code a successful call returns.
* Changing the scope an endpoint requires, removing a scope, or making a
  public endpoint require a credential.
* Renaming an `operationId`. This one surprises people: an `operationId` is
  derived from the handler function's name, so renaming
  `def list_agents(...)` to `def get_agents(...)` renames the method in every
  generated SDK. Rename the handler only when you mean to.

Changing prose — a description, a summary, a tag's blurb — is never breaking.

## Deprecation

When something in `v1` genuinely has to go, it goes slowly and audibly:

1. **Announce.** A `### Deprecated` entry in `CHANGELOG.md` under the release
   that introduces the replacement, naming the endpoint or field, the
   replacement, and the earliest release in which it may be removed.
2. **Mark.** The endpoint is marked `deprecated: true` in the OpenAPI document
   (`@router.get(..., deprecated=True)`), which renders as a badge in
   `/api-docs`. A deprecated *field* keeps working and says so in its
   description.
3. **Signal at runtime.** Deprecated endpoints answer with:

   ```
   Deprecation: true
   Sunset: Wed, 01 Jul 2026 00:00:00 GMT
   Link: <https://your-jhin-host/api-docs#the-replacement>; rel="deprecation"
   ```

   `Deprecation` and `Sunset` are the IETF headers (RFC 8594); a client can
   watch for them without reading a changelog.
4. **Wait.** The minimum support window is **two minor releases and at least
   90 days** after the release that announced the deprecation, whichever is
   longer. A self-hoster who upgrades twice a year gets a warning before
   anything they use stops answering.
5. **Remove — in `v2`, not `v1`.** The support window is the notice period for
   the *replacement being available*, not permission to delete from `v1`.
   Removal happens when `v1` itself is retired.

## How `/api/v2` would arrive

`v2` is introduced **beside** `v1`, never in place of it:

* New routers mount under `/api/v2`. `/api/v1` keeps its own routers, its own
  schemas, and its own snapshot (`docs/api/openapi.v2.json` joins
  `openapi.v1.json`; both are checked).
* `GET /api/v1/health` keeps reporting `api_version: "v1"`; the `v2` health
  endpoint reports `"v2"`. An integrator probes both to discover what an
  install supports.
* API keys and scopes are shared: a key is a workspace credential, not a
  version's credential.
* `v1` is supported for at least **12 months** after `v2` ships, and its
  retirement is announced in `CHANGELOG.md` and in the reference before it
  happens.

Nothing about this is hypothetical for the reader: if `/api/v1` still answers,
your integration still works.

## What stops a break from shipping

Prose is not a guarantee. Two mechanisms make the rules above enforceable.

### The scope shown is the scope enforced

The reference does not restate permissions; it reads them out of
`apps/api/src/jhin_api/access/route_scopes.py`, the single table
`require_workspace_role` already consults on every request. Each operation in
the document carries an `x-jhin-scope` extension and a security requirement
built from that table, and `test_openapi_metadata.py` asserts the two agree
for every workspace route. A scope cannot change in the API without changing
in the document, and cannot change in the document without changing in the
API.

### The snapshot test

`scripts/openapi_snapshot.py` regenerates the document from the app and
compares it with `docs/api/openapi.v1.json` — not for equality, but by
resolving both documents into a comparable surface and classifying every
difference:

* every path and operation, so removals are caught;
* every request and response body, flattened through `$ref`s (with a cycle
  guard for self-referential schemas like the org tree) into `dotted.field`
  leaves carrying type, requiredness, and enum values;
* every parameter, status code, security requirement, and `operationId`.

Each difference is marked breaking or not, per the two tables above. The test
in `tests/test_openapi_snapshot.py` then asserts two things:

1. **No breaking change** against the snapshot. This fails the build, prints
   the specific fields, and tells you to add beside rather than change in
   place.
2. **The snapshot is current.** A compatible change fails too, with a
   different message: run `uv run python scripts/openapi_snapshot.py --update`
   and commit the diff. This is deliberate — an additive change should still
   be *visible in review*, which is the whole reason the file is committed.

The same test file covers the detector itself against crafted document pairs,
one per row of both tables, so the classifier cannot quietly rot into a
comparison that passes everything.

Both run in `uv run pytest -q`, which CI runs on every push and pull request.
The release preflight (`scripts/release_preflight.py`) additionally checks
that the snapshot exists and that its `info.version` matches `VERSION`, so a
release cannot ship with a snapshot from a different build.

### Working with the gate

```bash
# See what changed and how it is classified
uv run python scripts/openapi_snapshot.py

# Accept a compatible change
uv run python scripts/openapi_snapshot.py --update

# Write the current document somewhere else (SDK generation, review)
uv run python scripts/openapi_snapshot.py --print > /tmp/openapi.json
```

If the tool reports a breaking change and you believe the break is correct —
a deprecation whose window has expired, or `v2` arriving — update the snapshot
deliberately and say so in `CHANGELOG.md`. The gate is there to make that a
decision, not an accident.
