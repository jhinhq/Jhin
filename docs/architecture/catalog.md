# The app and skill catalog

Jhin ships with 50 hand-curated apps. The catalog is the other several
thousand: a periodically refreshed index of public MCP servers and agent
skills, built upstream by crawling the registries and published as a GitHub
Release. This document is the security model and the operational contract for
keeping a local copy of it.

The index is **an index**. Nothing in it is connected to anything until
somebody in a workspace presses Connect, no code path dials an indexed
`mcp_url`, and no catalog text is ever placed in an agent prompt or a tool
definition. The catalog is what a person browses; it is not what a model
reads.

## What is stored

Two global tables — the first in the schema besides `user`/`user_session`
that carry no `workspace_id`, because the catalog is public reference data
rather than workspace content.

| Table | Row | Notes |
| --- | --- | --- |
| `catalog_version` | one generation | release tag + archive digest, `loading` → `active` → `superseded` (or `failed`), plus the shard cursor a resumed load reads |
| `catalog_entry` | one indexed server or skill within one generation | every string already redacted, control-stripped, and capped at ingest; the column widths are the second gate |

The 50 built-in `CatalogApp` entries are **not** in these tables. They are
merged in at read time, always sort before every synced row, and their slugs
are reserved against synced rows at three independent gates, alongside two
more that close the same impersonation through the trust tier and the
connector type (see [Impersonation](#impersonation)).

## Generations, and why a reader never sees half a catalog

A refresh does not mutate rows a reader might be looking at. It writes a
whole new generation and then flips a pointer:

```mermaid
sequenceDiagram
  participant J as jhin-catalog-sync
  participant DB as PostgreSQL
  participant R as a reader (API)
  J->>DB: INSERT catalog_version (status='loading')
  loop mcp/00 … skills/ff
    J->>DB: INSERT … ON CONFLICT (version_id, canonical_key) DO UPDATE
    J->>DB: UPDATE shard_cursor = :shard ; COMMIT
    R->>DB: SELECT id WHERE status='active'  →  still the old generation
  end
  J->>DB: BEGIN
  J->>DB: UPDATE catalog_version SET status='superseded' WHERE status='active'
  J->>DB: UPDATE catalog_version SET status='active' WHERE id=:new
  J->>DB: COMMIT
  R->>DB: SELECT id WHERE status='active'  →  the new generation, complete
```

Readers resolve the active version id first and filter every query on it. A
`loading` generation is never active, so a reader mid-refresh sees the
previous catalog **in full** rather than the new one in part. There is no
window in which both are visible and none in which neither is.

`uq_catalog_version_active` — a unique index on `status` restricted to
`status = 'active'` — makes "exactly one active generation" a schema fact
rather than a convention. It is also what serialises two syncs racing to
publish: the loser's swap fails, it exits non-zero, and the winner's
generation is untouched.

**Idempotent.** The identity of a generation is `(release_tag,
data_sha256)`. Running the sync against a release that is already active
returns immediately and writes nothing.

**Resumable.** `shard_cursor` records the last *completely* loaded shard. A
run that dies picks up after it. A shard that was half-written when the
process died is replayed, which the `ON CONFLICT DO UPDATE` makes free.

**Pruned.** The active generation plus the newest two inactive ones are
kept; the rest are deleted with their entries. Two are enough to see what
the last refresh replaced and to roll back to the previous release by
re-activating it rather than re-downloading it.

## The fetch boundary

`jhin_catalog_sync.fetch` is the only code in Jhin that talks to the catalog
host. It keeps the posture of the connector HTTP clients
(`jhin_connectors.http_client`):

| Control | Value |
| --- | --- |
| Release metadata cap | 256 KiB |
| `SHA256SUMS` cap | 4 KiB |
| Archive cap | 32 MiB |
| Timeouts | connect 10s, read 60s, write 10s, pool 10s |
| Whole-sync wall clock | 300s over the network phase |
| Redirects | refused, with one narrow exception (below) |
| Errors | stable strings; never a URL, a header, or a byte of upstream text |

Both the **declared** `Content-Length` and the **streamed** byte count are
checked, because a server that lies about its length is exactly the server
being guarded against. The sha256 is computed over the bytes as they stream
past, never over a re-read.

### The one redirect

GitHub serves release assets only as a `302` into its object store, so
exactly one hop is followed, and only when the `Location` is an absolute
`https` URL whose host is one of:

- `objects.githubusercontent.com`
- `release-assets.githubusercontent.com`
- `github-releases.githubusercontent.com`

The hop is issued as a freshly constructed request rather than a copy of the
first one, so the client's default headers — an `Authorization` among them —
never reach the object store, and the byte cap is applied again on the new
stream. Any other 3xx, and any second hop, raises. This is a deliberate,
documented exception to the repo-wide no-redirect rule and the only one.

### Integrity before any write

`download_verified_archive` fetches `SHA256SUMS` **first**, so the expected
digest is fixed before the bytes it describes exist in this process, then
compares it against the streamed digest of
`jhin-catalog-<tag>-data.tar.gz`. A mismatch raises. `cli.sync_once`
sequences fetch → verify → open → load, so the loader is unreachable with
unverified bytes.

**This is integrity, not provenance, and the distinction is the whole
threat model below.** `SHA256SUMS` is fetched from the same release, on the
same host, over the same channel as the archive it describes. That proves the
bytes arrived intact — no truncation, no corruption, no tampering in transit.
It proves nothing about *who* published them. Nothing here verifies a
signature. A compromised `jhinhq/jhin-catalog`, a malicious release
publisher, or a `CATALOG_SOURCE_REPO` pointed at an attacker's fork all
produce an archive that passes this check by construction.

So every guarantee in this document assumes an attacker may control any row
in the index, and the gates below — reserved slugs, the unassertable
`curated` tier, no native `connector_type` on a synced row, the server-built
config schema — are what stand between a hostile row and a workspace. They
are load-bearing precisely because the integrity check is not a trust check.

## The archive boundary

A tarball from the internet is an old attack surface, so Jhin never
extracts one. `open_archive` reads members one at a time through
`extractfile` into bounded `bytes`; no path is ever joined, opened, or
created. A member is refused unless it is a regular file whose name
fullmatches

```
^(data/(mcp|skills)/[0-9a-f]{2}\.jsonl|sources\.lock|schema/catalog\.schema\.json)$
```

which rejects traversal, absolute paths, symlinks, and anything unexpected
in one test. Three more ceilings sit behind it: 8 MiB per member, 192 MiB
uncompressed in total, and 600 members. A missing shard is tolerated — the
upstream build omits empty ones — but a missing `sources.lock` is not.

## What a record becomes

Each line of a shard is one JSON record. `jhin_catalog_sync.wire` validates
it against the wire models and projects it onto a `catalog_entry` row:

- every free-text field goes through `clean_text` — secret redaction via the
  process redactor, control characters dropped, whitespace collapsed, capped
  at the column width;
- `mcp_json` / `skill_json` pass through `jhin_tools.sanitize.sanitize_payload`
  at 512 chars per string and 8 KiB per document;
- a line that is too long, mis-sharded, of the wrong kind, of an unsupported
  `schema_version`, or simply invalid is **rejected and counted**, never
  fatal — one bad row upstream must not cost the whole refresh.

`config_schema` is **not** taken from catalog data. The API builds it at
read time from installed connector manifests; the catalog supplies values
for known field names only, never field definitions. That removes the
schema-injection vector rather than filtering it.

## Trust tiers and the risk floor

`trust_tier` records **provenance**, not observed behaviour, and maps to a
risk floor a connection made from the entry starts at:

| `trust_tier` | rank | `default_risk` | resulting action | shown as |
| --- | --- | --- | --- | --- |
| `curated` | 0 | `write` | auto | Reviewed by Jhin |
| `registry_verified` | 1 | `write` | auto | Listed in the official MCP registry |
| `smithery_verified` | 2 | `elevated` | approval | Verified by Smithery — approve each use |
| `indexed` | 3 | `elevated` | approval | Found by crawling — approve each use |

One modifier: when the endpoint is unverified and the tier is not `curated`,
the result is raised to `elevated`. `default_risk` is never `read` and never
`destructive` — the catalog knows where a server came from, not what its
tools do.

**`curated` is not a tier the sync can write.** Every other value in that
table is upstream describing itself, which is a claim the floor above is
deliberately blunt about. `curated` is different: it means a person at Jhin
reviewed the entry, and the only entries that is true of are the built-ins
compiled into `jhin_connectors`. It is also the one row of the table that
*pays* — note the modifier exempts `curated`, so an index asserting it about
a server whose endpoint nobody could reach would buy `write` where the truth
earns `elevated`, on top of the reassuring badge and the top of the trust
sort. So `risk.syncable_tier` demotes the claim to `indexed` at ingest, and
`_conditions` refuses to serve a row still carrying it — a row that does is
not one this sync wrote. The read path recomputes the floor from the
sanitised tier rather than trusting the stored `default_risk` column.

`POST /workspaces/{id}/catalog/apply-risk-floor` writes the floor into
`connection.config_json["tool_risk_overrides"]`, the same key the existing
tools endpoint writes and the tool worker already reads. Nothing in the
connections module, the policy engine, or the gateway changes. A tool
already at or above the floor is left alone, so the action is idempotent and
never lowers a risk an admin raised.

## Impersonation

A crawled server must never be able to appear as a reviewed one. Three
independent gates:

1. **At ingest** — `wire.safe_slug` rewrites any synced slug that collides
   with a built-in curated slug into a deterministic, digest-suffixed
   variant.
2. **At read time** — the API resolves built-ins first and drops any synced
   row whose slug is in `builtin_slugs()`.
3. **In the schema** — `uq_catalog_entry_version_id_kind_slug` makes a
   duplicate unrepresentable inside a generation.

The slug is not the only column a crawled row could wear a reviewed face
through, so two more gates close the same theft by another route:

4. **The tier** — a synced row may not claim `curated` (above).
5. **The connector** — a synced row's `connector_type` is never projected.
   `config_schema._manifest_for` resolves that column against the *installed*
   registry, so a row naming `github` would otherwise render GitHub's real
   Connect form — GitHub's auth schemes, GitHub's fields — under a name and
   icon the index chose, and `connectionsForApp` would badge it **Connected**
   off an unrelated GitHub connection. A crawled entry is an MCP server and
   is served as one; the generic MCP manifest is what `_manifest_for` already
   documents as the graceful fallback. `connectable` carries the matching
   kind gate, so a *skill* is never offered a Connect button either — a skill
   is installed, not connected, and `_config_schema` correctly builds no form
   for one.

Every response also carries `source: "builtin" | "synced"`, so the UI can
never confuse the two. Within a generation, two unrelated upstream entries
wanting the same slug is a real possibility; the loader drops the second and
counts it as rejected rather than letting a unique-constraint violation take
the whole refresh down.

## Running it

The refresh is a cron job, not a service. There is no Temporal workflow and
nothing long-running.

```
jhin-catalog-sync [--database-url URL] [--repo owner/repo] [--tag TAG] [--json]
```

| Setting | Source | Default |
| --- | --- | --- |
| `--database-url` | `DATABASE_URL` | required |
| `--repo` | `CATALOG_SOURCE_REPO` | `jhinhq/jhin-catalog` |
| `--tag` | — | the latest release |

Exit codes, which are the interface a scheduler reads:

| Code | Meaning |
| --- | --- |
| `0` | the catalog is current, whether or not this run changed it |
| `3` | the archive failed its integrity check |
| `4` | the release could not be fetched |
| `5` | the archive or a shard was malformed |
| `1` | anything else, including a lost swap race |

With `--json`, exactly one canonical JSON object of the outcome is printed
on stdout; failures go to stderr and carry no upstream text.

In Compose the job is behind the `catalog` profile, so it is never part of
the running stack:

```
docker compose --profile catalog run --rm catalog-sync
```

Point cron at that line, nightly. Running it twice is harmless, running two
at once is harmless, and a run that dies resumes.

## Known limits

- The generic `mcp` connector is the only way to reach an indexed server
  that has no native Jhin connector, so `stdio_only` entries are listed for
  completeness and are never connectable from a hosted deployment.
- The whole archive is held in memory during a load. At the current scale
  (~1.2k–5k entries, a few MiB compressed) that is the simpler trade; the
  32 MiB fetch cap is what keeps it true.
- Search is a Postgres `GIN (to_tsvector('english', search_text))` index with
  an `ILIKE` fallback elsewhere, mirroring migration `0016`. `pg_trgm` is
  deliberately not introduced: at this row count a sequential `ILIKE` is
  sub-millisecond and a new extension would be the first in the repo after
  `vector`.
