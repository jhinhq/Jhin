# Agent Skills

Skills give agents reusable, operator-curated instruction packs — "how we
write release notes here", "our code review checklist" — that agents
discover by name and read on demand while they work.

This document is the implementation contract for the skills package, the
API, the gateway tools, the prompt block, and the web app. Code lives in
`packages/skills` (`jhin_skills`), `apps/api/src/jhin_api/skills`,
`packages/tools/src/jhin_tools/skills_tools.py`,
`packages/agents/src/jhin_agents/context.py` (`skills_block`), and
`services/agent_worker/src/jhin_agent_worker/skills_activities.py`.

## Format: the open Agent Skills convention

Jhin stores and exchanges skills in the open **Agent Skills** format used
by Claude and published at github.com/anthropics/skills: a folder holding a
`SKILL.md` file — YAML frontmatter with `name`, `description`, and
optionally `license` and `allowed-tools`, followed by markdown
instructions — plus optional extra reference files next to it. Any skill
written for that ecosystem imports into Jhin unchanged, and a skill
authored in Jhin is a valid skill folder for it.

Two deliberate deviations:

- `allowed-tools` is parsed but **advisory only**. In Jhin, what an agent
  may call is decided exclusively by the tool gateway (grants, scopes,
  policy) — a text file never grants capability. It is accepted (and
  bounds-checked) on every creation path, including the `skills.create`
  gateway tool below, but never persisted — nothing in Jhin stores it.
- Frontmatter parsing is a minimal, bounded reader of the flat
  `key: value` mappings the format uses — flow lists (`[a, b]`), block
  lists, **block scalars** (folded `>` and literal `|`, with chomping
  (`>-`, `|+`) and explicit-indent indicators), and the backslash escapes a
  double-quoted scalar uses — capped at 8 KB. No YAML engine, no tags, no
  anchors. A skill that still fails to parse is skipped (with a warning,
  where the caller surfaces warnings) exactly as an import would skip it.

  Block-scalar support was added after live verification showed it was
  silently corrupting real skills: `anthropics/skills`' `academy-guide` and
  `discernment-nudge` use `description: >` and were landing with the literal
  description `">"`.

Validation (`jhin_skills.parser`): `name` is a slug (lowercase letters,
digits, hyphens, ≤ 64 chars; defaults to the folder name), `description`
is required and ≤ 2000 chars, the body is ≤ 64 KB, each reference file is
≤ 64 KB, a skill totals ≤ 256 KB across ≤ 20 files, and obviously
credential-like content (private keys, provider API keys, tokens) is
rejected outright. Every creation path — the plain create API, a GitHub
import, a browse-gallery install, and the `skills.create` gateway tool —
runs the same `jhin_skills` primitives, so an agent-authored skill obeys
exactly the same rules as a human-authored or imported one.

## Data model (migration `0022`, extended by `0023`, `0024`, `0025`)

### `skill`

| column | type | notes |
| --- | --- | --- |
| `id` | uuid pk | UUIDv7 |
| `workspace_id` | uuid fk workspace (cascade) | indexed |
| `name` | varchar(64) | slug; unique per workspace |
| `description` | varchar(2000) | shown to agents in the prompt (the prompt block truncates to 300; widened from 500 in `0025`) |
| `content` | text | the SKILL.md markdown body (frontmatter stripped) |
| `files_json` | json list | `[{"path", "content"}, ...]` reference files |
| `source` | varchar(16) | `built_in` / `imported` / `custom` / `agent_authored` |
| `source_url` | varchar(500) | provenance for imports and browse installs (`""` otherwise) |
| `category` | varchar(64), nullable | display grouping; `NULL` reads as "General" (added in `0024`, see below) |
| `enabled` | boolean | workspace-level switch |
| `version` | integer | bumped on content/file edits |
| `created_by_agent_id` | uuid fk agent (set null), nullable, indexed | set only for `source="agent_authored"` — which agent's `skills.create` call made this skill (added in `0023`) |
| `created_at`, `updated_at` | timestamptz | |

## Category taxonomy

Every skill carries a `category` used to group and filter the library. It is
never required at creation time — a missing value reads as `"General"`
(`jhin_skills.DEFAULT_CATEGORY`) — and it is always editable afterward via
`PATCH /skills/{id}`.

### How a browse install derives one

`jhin_skills.category.derive_category` runs a three-step cascade,
most-authoritative first. The first step that produces something wins:

1. **A category the skill declares itself** — a `category` key in the
   SKILL.md frontmatter, humanized. No repository in the current catalog
   ships one, but honoring it costs nothing and is the most accurate signal
   available when a repository does.
2. **The nearest meaningful ancestor folder** — walking up from the skill's
   own folder, skipping *generic container* segments
   (`GENERIC_FOLDER_SEGMENTS`: `skills/`, `src/`, `packages/`, `examples/`,
   `.github/`, `template/`, …), humanized. This is what gives a genuinely
   categorized repository its own categories:
   `document-skills/pdf` → "Document skills", `legal/skills/contracts` →
   "Legal".
3. **A small fixed keyword taxonomy** over the skill's name and description
   (`CATEGORY_KEYWORDS`): Documents, Data & analysis, Design & creative,
   Testing & QA, Security, Operations, AI & agents, Engineering,
   Communication, Product & planning, Learning & enablement. Every entry is
   scored — a keyword hit in the *name* counts triple, since the name is the
   stronger signal — and the highest scorer wins, ties going to the earlier
   (more specific) entry. Matching is **whole-word** (with tolerance for
   `s`/`es`/`ing`/`ed` inflections), not substring: plain substring matching
   put "art" inside "artifacts" and "test" inside "latest".

Anything still unmatched falls back to `"General"`.

**Why step 3 exists.** Deriving purely from folder structure — the obvious
rule — is useless against the actual repositories in the catalog. Both
`anthropics/skills` and `obra/superpowers` nest *every* skill under one
generic `skills/` wrapper, so "humanize the parent folder" put all of them
in a single bucket named "Skills". Step 2 deliberately refuses to return a
generic wrapper, and step 3 then classifies from the only real signal a flat
repository carries: the name and description.

The distribution this actually produces, measured against the live
repositories:

| source | skills | categories produced |
| --- | --- | --- |
| `anthropics/skills` | 19 | Design & creative 6, Documents 4, AI & agents 2, Engineering 2, Learning & enablement 2, Communication 1, Testing & QA 1, Data & analysis 1 |
| `obra/superpowers` | 14 | Engineering 5, AI & agents 4, Product & planning 3, Testing & QA 2 |

### Everywhere else

- **Built-in starters** carry hand-picked categories:
  `writing-clear-updates` → Communication, `code-review-checklist` →
  Engineering, `bug-report-triage` → Support, `meeting-notes-summary` →
  Communication, `release-notes` → Engineering.
- **Manual create, a raw GitHub/zip import, and agent authoring
  (`skills.create`)** all default to `"General"`, editable afterward.
  Deriving from a repository is limited to the browse gallery, where the
  admin is choosing a specific folder inside a known repository.

### Repairing existing workspaces

A workspace whose starters were installed before the `category` column
existed carries `NULL` on all five, which reads as "General". Two paths fix
that without any manual editing:

- migration `0025` backfills the five starters' categories, scoped to rows
  that are still `source = 'built_in'` and still `NULL`;
- `POST /skills/install-builtins` ("install missing defaults") doubles as
  "repair the defaults": alongside installing whatever is missing, it
  corrects the category of a starter that is present but mis-categorized.
  It only ever touches a row that is still `source = 'built_in'`, and only
  its `category` — an admin who replaced a starter with their own skill of
  the same name keeps everything, category included.

This is a deliberate, narrow exception to the rule that a migration never
retroactively changes a workspace's skills: a display grouping on a
Jhin-shipped starter is metadata, not content.

There is no separate `GET /skills/categories` endpoint: `GET /skills`
already returns every skill's `category`, so the web app derives the
distinct-category list and groupings client-side
(`apps/web/lib/skills.ts`'s `categoriesOf` / `groupByCategory`) rather than
paying a second round trip for data already in hand. The browse gallery does
the same over its listing response.


## Progressive disclosure at runtime

1. **Prompt block** — for each reasoning step the agent worker loads the
   agent's enabled skills (workspace-enabled AND agent-enabled) in its own
   best-effort session (`skills.context_failed` on error → no block) and
   renders `jhin_agents.context.skills_block`: a bounded "Skills available
   to you" list of `name — description` lines (≤ 50 skills, descriptions
   truncated) plus the instruction to read a skill before using it.
2. **`skills.read` gateway tool** — read-risk, capability `skills.read`,
   scope key `name` (so a grant can pin an fnmatch pattern like
   `release-*`). Returns the skill's markdown instructions, its reference
   file list, and `version`; `file` fetches one reference file. Output is
   bounded to 24 KB per call with an explicit `truncated` flag (below the
   gateway's 32 KB sanitizer cap).

`skills.read` is **not granted by default**. The agent wizard offers a
one-click "Skills" preset that grants `skills.read` for every skill
(`name: *`); admins can narrow it like any other grant.

## Skills library defaults for new workspaces

Every newly created workspace starts with the five starter skills already
installed **and enabled** — not proposed, not a manual step. This is
staged in the same database transaction as workspace creation itself
(`jhin_api.skills.service.install_builtins_for_new_workspace`, called from
both real owner-facing creation paths: `POST /api/v1/workspaces` and the
first-run owner bootstrap flow), so a workspace never exists, even
momentarily, without its starters. The audit event carries
`metadata.source: "default"` to distinguish it from an admin's manual
click.

Existing workspaces are **never** touched retroactively by a migration —
that would be a surprising, unannounced content change to a workspace an
admin may have deliberately pruned. Instead, `POST /skills/install-builtins`
(the existing "Install starter skills" button — admin, idempotent, skips
any starter name already present) doubles as "install missing defaults":
calling it on a workspace that already has some or all starters only adds
what is missing, tagged `metadata.source: "manual"`. Both paths share one
underlying function (`_install_builtins_core`); only the audit metadata and
the (missing, at creation time) admin-permission check differ.

## The browse gallery: a live, searchable skills catalog

`GET /api/v1/workspaces/{workspace_id}/skill-sources` returns the
**hardcoded default catalog** (`jhin_api.skills.service.SKILL_SOURCES`) plus
this workspace's own **custom sources** (see below). This is only "where to
look": no skill content is bundled or vendored into Jhin: on every browse
call, the actual repository is fetched live over the exact same
`codeload.github.com` zip mechanism `POST /skills/import` already uses
(`jhin_skills.fetch_github_repo_zip` + `load_zip`).

### The default catalog

Every entry below was verified live during development — fetched over the
real `codeload.github.com` zip endpoint, loaded through
`jhin_skills.bundle.load_zip`, and confirmed to parse at least one valid
`SKILL.md` with the app's actual (bounded, non-YAML) frontmatter parser —
never added on the strength of a description alone:

| source | what it is | verified |
| --- | --- | --- |
| `anthropics/skills` | Anthropic's official public Agent Skills library | 19 skills parse cleanly (16 before block-scalar support and the widened description cap); `claude-api` remains out — see below |
| `obra/superpowers` | An agentic skills framework and software-development methodology (TDD, systematic debugging, code review, git worktrees, …) — referenced by this codebase's own `docs/superpowers` naming convention, and a real, independently-verified public repository | 14 skills parse cleanly, zero warnings |
| `addyosmani/agent-skills` | Production-grade engineering skills for AI coding agents | 24 skills parse cleanly, zero warnings |
| `jamestorrevillas/dev-skills` | A modular skill library for software engineers — technical, soft, and career skills | 37 skills parse cleanly, zero warnings (nested under `.github/skills/`) |
| `avizmarlon/agent-skills` | Portable agent skills shared across Claude Code, Codex, Cursor, and Gemini | 31 skills parse cleanly, zero warnings |

One skill in `anthropics/skills` is still not installable:
**`claude-api`**, whose SKILL.md is 75 707 bytes — its *body alone* is
74 542 bytes, past the 64 KB `MAX_CONTENT_BYTES` cap. That is a genuine
size limit, not a parser gap: block-scalar support fixed its frontmatter but
cannot shrink its body. It is dropped at the zip-entry level, before it ever
becomes a skill folder, so it produces no warning.

Candidates that were checked live and **rejected**, for the record — every
one failed one of this app's own real, enforced constraints, not a
subjective judgment call:

- `TerminalSkills/skills` — real and skill-rich (1019 `SKILL.md` files), but
  its zip archive is ~8 MB, over the 5 MB cap `fetch_github_repo_zip`
  enforces (the same cap a live install would hit) — not fetchable through
  this app as a whole-repo source.
- `bregman-arie/devops-sre-skills` — real and fetchable, but every one of
  its 17 `SKILL.md` files uses a Title Case `name:` (e.g. `"Triage AWS
  AccessDenied"`); the parser's slug rule rejects all of them, so it yields
  **zero** usable skills.
- `ComposioHQ/awesome-claude-skills` and `ellmos-ai/skills` — real, but one
  holds over the 50-skills-per-bundle cap (`load_zip` refuses a whole-repo
  browse outright) and the other's archive is ~35 MB, over the 5 MB fetch
  cap.

Extend `SKILL_SOURCES` with the same care as adding a new built-in — every
entry here is treated as maintainer-reviewed, which is what lets a browse
install skip the "review and enable" queue (see below).

### Workspace-custom sources

An admin can add their own source directly from the Browse library tab
("Add a source"): `owner/repo`, optionally `/path`. It is **validated
live** the moment it's added — fetched via the same `fetch_github_repo_zip`
+ `load_zip` path a browse already uses, and rejected with a clear reason
if it doesn't fetch or contains no `SKILL.md` this parser accepts — so
nothing unfetchable or empty is ever persisted.

Custom sources are stored per workspace at
`workspace.settings_json["skill_sources"]`: a small JSON list
(`{"source", "label", "description", "url", "added_by", "added_at"}` per
entry) rather than a dedicated table, matching how other low-cardinality,
admin-curated workspace configuration (budgets, coordination limits)
already lives there. `POST /skill-sources` (admin) validates and appends
one; `DELETE /skill-sources/{source}` (admin) removes one — only a custom
entry can be removed; a default is not stored per-workspace and 404s. A
workspace-custom source is treated exactly like a default one everywhere
else — `GET /skills/browse`, `POST /skills/browse/install`, and category
derivation all work identically over it.

`GET /skills/browse?source=<owner/repo>&q=<text>` (viewer+) fetches that
source's zip once, parses every `SKILL.md` found anywhere in the tree
(the loader already walks the whole archive regardless of nesting depth —
`anthropics/skills` itself nests every skill one level under `skills/`,
confirmed with a live fetch during development), and returns
`{name, description, path, installed}` for each, filtered by `q` against
name and description. The parsed listing is cached **in-process, per
source, for 10 minutes** (`jhin_api.skills.service._browse_cache`) so
rapid search keystrokes re-filter an already-parsed listing instead of
re-fetching and re-parsing the zip on every request. A skill already
present in the workspace is marked `installed: true`, matched by
`(name, source_url)` — the same provenance URL a browse install below
would have stored.

`POST /skills/browse/install` `{source, skill_path}` (admin) installs
**exactly one** skill folder: it reuses the same single-skill fetch/parse
path (`fetch_github_repo_zip` scoped to `source/skill_path`, then
`load_zip`), not the whole-repo import flow — no other skill in the
repository is touched or proposed. Idempotent: a retry of the same
`(source, skill_path)` returns the existing record instead of erroring or
duplicating; a name collision with a *different* source is a 409, same as
the plain create API.

### Design decision: browse installs are enabled immediately

A raw `POST /skills/import` of an arbitrary admin-typed `owner/repo` lands
every skill it finds as `enabled=false`, awaiting review — the admin has
not seen the content yet, and importing pulls in everything the repo
contains sight-unseen. A browse-gallery install is different on both axes
that make review necessary:

- **the source is a known, single entity, not arbitrary** — only sources in
  `GET /skill-sources` (the hardcoded catalog, a maintainer-reviewed public
  library, plus this workspace's own custom additions) can be browsed or
  installed from at all;
- **the admin already read it** — browsing shows the name and description
  before any action, and install targets exactly the one skill folder the
  admin picked, never a bulk import of the whole repo.

Given both, gating the result behind a second manual "review and enable"
step would add friction without adding safety, so a browse install is
`enabled=True` from the moment it lands (`source="imported"`,
`source_url` pointing at the specific skill folder, audited as
`skill.browse_installed`). This applies equally to a workspace-custom
source: it carries a weaker trust claim than a hardcoded default (an admin
picked it, not a Jhin maintainer), but the same admin who could add it
could equally run a raw `owner/repo` import and review it manually — and
browsing already shows the exact skill before install, same as the default
catalog. Extending `SKILL_SOURCES` (the hardcoded, maintainer-reviewed
tuple) to a repository that isn't genuinely maintainer-curated would
silently change what "default" implies — treat additions to that tuple
with the same care as adding a new built-in; workspace-custom additions are
explicitly a lower, admin-scoped trust tier and are labeled "(custom)" in
the web UI so that distinction stays visible.

## Agents can author skills through chat

Two gateway tools, capability **`skills.manage`** (elevated risk,
approval-gated by default — same posture as `organization.create_agent`,
for the same reason: this creates persistent workspace configuration other
agents may come to read):

- **`skills.create`** `{name, description, content, files?, allowed_tools?}`
  creates a new skill directly as `enabled=true, source="agent_authored",
  created_by_agent_id=<caller>`. No separate review gate — the human
  already approved the tool call itself, which is why the risk level is
  elevated (approval-gated) rather than write (auto-approved): the human
  in the loop *is* the review. Validation reuses the exact `jhin_skills`
  primitives (size caps, name slug, secret screen) the plain API and
  import paths use.
- **`skills.update`** `{skill_id or name, description?, content?, files?}`
  revises an existing skill, but **only one the calling agent itself
  authored with `skills.create`** — enforced by a registered gateway
  validator (`validate_skills_update`, denied before it ever reaches
  approval) plus a defense-in-depth recheck in the executor, mirroring how
  `organization.update_agent_profile` restricts `system_prompt` edits to a
  caller in the target's manager chain. A human-authored, imported,
  built-in, or a *different* agent's authored skill is out of reach here —
  full stop. Agents can never enable, disable, or delete any skill through
  these tools; that stays human/admin-only via the existing CRUD API.

### Design decision: the wizard grant

The agent wizard's existing "Skills" preset only grants `skills.read`
(auto-approved, read-only). `skills.manage` is elevated, approval-gated,
and mutates workspace configuration — bundling it into "Skills" would mean
an admin who just wants an agent to *read* the library also silently grants
it authoring power. Rather than overload one preset with two very
different trust levels, a second, explicit **"Skill authoring"** preset
grants `skills.create` + `skills.update` (both map to the one
`skills.manage` capability) on its own, so an admin opts in deliberately
and the wizard card names the exact behavior: every call needs approval,
and it can only ever touch skills the agent itself wrote.

### Approval card readability

The gateway's approval payload is the tool call's exact validated input
(so a resumed approval can replay byte-for-byte) — for `skills.create` /
`skills.update` that includes the full skill body, which reads poorly as a
raw JSON dump and, past 8 KB, cannot even be parked for approval at all
(the gateway denies outright with `approval_input_not_lossless` once a
field exceeds its sanitizer's per-string cap — a pre-existing, general
constraint on every approval-gated tool with a large text field, not
something introduced here). The web approvals inbox special-cases these
two action types (`apps/web/components/approval-card.tsx`) to render just
the skill's `name` plus a ~200-character content preview instead of the
full JSON, without touching what is actually persisted or replayed.

## Security model

- **Admin-curated**: creating, editing, enabling, deleting, importing, and
  per-agent enablement are admin-only (viewers read the library; the API
  additionally enforces admin in the service layer). Skill content is
  therefore operator-approved instruction text — it enters the prompt as
  ordinary curated context, not labeled untrusted. An agent-authored skill
  is the one exception, and only because a human approved its creation via
  the tool-call approval gate; it carries the same trust as any other
  skill from that point on.
- **Import review**: `POST /skills/import` (GitHub `owner/repo[/path]`
  fetched via the `codeload.github.com` zip over HTTPS, redirect-free,
  ≤ 5 MB) and `POST /skills/import-zip` (multipart upload) create skills
  as `enabled=false` "proposed" entries. Nothing reaches an agent until an
  admin reviews and enables each one. A browse-gallery install is the one
  deliberate exception — see above.
- **Size caps everywhere**: 5 MB per archive, 64 KB per document, 256 KB
  per skill, 50 skills per bundle, bounded frontmatter, bounded tool
  output.
- **Secret screening**: skill bodies and files are scanned for obvious
  credential patterns on create, update, and import (including agent
  authoring); matches are rejected (never stored-then-redacted).
- **Audit**: `skill.created` / `skill.updated` / `skill.enabled` /
  `skill.disabled` / `skill.deleted` / `skill.builtins_installed` (with a
  `source: "default" | "manual"` distinction) / `skill.imported` /
  `skill.browse_installed` / `agent.skills_updated` /
  `skill_source.added` / `skill_source.removed`, all content-free.

## API

Under `/api/v1/workspaces/{workspace_id}`:

- `GET /skills`, `GET /skills/{id}` — viewer+; list returns summaries
  (including `category`), detail includes body and files. `GET /skills`
  takes an optional `category` filter alongside `q` and `source`.
- `POST /skills`, `PATCH /skills/{id}`, `DELETE /skills/{id}` — admin.
  Create and update both accept an optional free-text `category` (defaults
  to `"General"` on create; omitted on update means "leave unchanged").
- `POST /skills/install-builtins` — admin; idempotently installs whichever
  of the five shipped starters (`writing-clear-updates`,
  `code-review-checklist`, `bug-report-triage`, `meeting-notes-summary`,
  `release-notes`) are still missing, which live as real skill folders in
  `packages/skills/src/jhin_skills/builtins`. New workspaces already have
  all five from creation; this is "install missing defaults" for existing
  ones.
- `POST /skills/import`, `POST /skills/import-zip` — admin; see above.
- `GET /skills/browse`, `POST /skills/browse/install` — the browse
  gallery, see above (viewer / admin respectively); browse entries carry a
  computed `category` too.
- `GET|PUT /agents/{agent_id}/skills` — viewer reads, admin replaces the
  agent's enabled set (`{"skill_ids": [...]}`); each entry carries the
  skill's `category`.
- `GET /skill-sources` (viewer+), `POST /skill-sources` (admin, live
  validated), `DELETE /skill-sources/{source}` (admin, custom only) — the
  browse catalog: defaults plus this workspace's own additions (see
  above).

There is no top-level, cross-workspace `/api/v1/skill-sources` any more —
custom sources are per-workspace, so the catalog moved under the workspace
prefix alongside everything else skills-related.

## Web

The **Skills** page (primary navigation, after Apps) has two sections:

- **Library** — install starters, import from GitHub or a zip, create and
  edit skills, toggle and delete (imported skills carry a "review and
  enable" banner). Skills are grouped into collapsible sections by
  `category` (an "General" bucket last), with a chip row above the list to
  filter down to one category at a time. The editor dialog has a free-text
  Category field with autocomplete (a `<datalist>`) built from the
  workspace's existing categories — type an existing one or a new one.
- **Browse library** — a source picker (the default catalog plus any
  workspace-custom sources, each labeled "(custom)"), an "Add a source"
  action that validates a new `owner/repo[/path]` live and gives a friendly
  error on failure, and a search box over the live gallery. Admins can
  remove a selected custom source. Results are grouped into the same
  category sections as the library, computed for display without
  installing anything (already-installed skills show disabled, labeled
  "Installed"; GitHub being unreachable shows a friendly inline error, not
  a crash).

The agent profile's **Skills** tab lets admins pick which library skills
the agent carries, with a category badge per skill and a hint when the
agent lacks a `skills.read` grant. The wizard's Tools & Access step offers
two skills-related presets: "Skills" (`skills.read`, read-only) and "Skill
authoring" (`skills.create` + `skills.update`, elevated/approval-gated).
