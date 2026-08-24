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
  `key: value` mappings the format uses (flow and block lists included),
  capped at 8 KB. No YAML engine, no tags, no anchors — block scalars
  (`description: |-`) are not understood, so a real-world skill using one
  (e.g. `anthropics/skills`' `claude-api`) treats everything after the
  scalar's opening line as unmodeled frontmatter and often ends up with an
  over-long body that trips the 64 KB content cap. This is a pre-existing,
  accepted limitation of the parser, not something the browse gallery works
  around: a skill that fails to parse is skipped (with a warning, where the
  caller surfaces warnings) exactly as an import would skip it.

Validation (`jhin_skills.parser`): `name` is a slug (lowercase letters,
digits, hyphens, ≤ 64 chars; defaults to the folder name), `description`
is required and ≤ 500 chars, the body is ≤ 64 KB, each reference file is
≤ 64 KB, a skill totals ≤ 256 KB across ≤ 20 files, and obviously
credential-like content (private keys, provider API keys, tokens) is
rejected outright. Every creation path — the plain create API, a GitHub
import, a browse-gallery install, and the `skills.create` gateway tool —
runs the same `jhin_skills` primitives, so an agent-authored skill obeys
exactly the same rules as a human-authored or imported one.

## Data model (migration `0022`, extended by `0023`)

### `skill`

| column | type | notes |
| --- | --- | --- |
| `id` | uuid pk | UUIDv7 |
| `workspace_id` | uuid fk workspace (cascade) | indexed |
| `name` | varchar(64) | slug; unique per workspace |
| `description` | varchar(500) | shown to agents in the prompt |
| `content` | text | the SKILL.md markdown body (frontmatter stripped) |
| `files_json` | json list | `[{"path", "content"}, ...]` reference files |
| `source` | varchar(16) | `built_in` / `imported` / `custom` / `agent_authored` |
| `source_url` | varchar(500) | provenance for imports and browse installs (`""` otherwise) |
| `enabled` | boolean | workspace-level switch |
| `version` | integer | bumped on content/file edits |
| `created_by_agent_id` | uuid fk agent (set null), nullable, indexed | set only for `source="agent_authored"` — which agent's `skills.create` call made this skill (added in `0023`) |
| `created_at`, `updated_at` | timestamptz | |

### `agent_skill`

The per-agent enablement join table (`workspace_id`, `agent_id`,
`skill_id`, unique on agent + skill). Deny-by-default: no row means the
skill does not appear in that agent's prompt. Creating a skill — by any
path, including `skills.create` — never adds this row for anyone; an admin
(or, for `skills.update`, the authoring agent revising its own content)
still decides which agents carry it.

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

`GET /api/v1/skill-sources` returns a small **hardcoded catalog** of known
public skill repositories (`jhin_api.skills.service.SKILL_SOURCES`) —
currently just `anthropics/skills`, the official public library. This is
only "where to look": no skill content is bundled or vendored into Jhin: on
every browse call, the actual repository is fetched live over the exact
same `codeload.github.com` zip mechanism `POST /skills/import` already
uses (`jhin_skills.fetch_github_repo_zip` + `load_zip`).

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

- **the source is curated**, not arbitrary — only repositories in the
  hardcoded `SKILL_SOURCES` catalog (a maintainer-reviewed public library,
  today just Anthropic's own) can be browsed or installed from at all;
- **the admin already read it** — browsing shows the name and description
  before any action, and install targets exactly the one skill folder the
  admin picked, never a bulk import of the whole repo.

Given both, gating the result behind a second manual "review and enable"
step would add friction without adding safety, so a browse install is
`enabled=True` from the moment it lands (`source="imported"`,
`source_url` pointing at the specific skill folder, audited as
`skill.browse_installed`). Extending `SKILL_SOURCES` to a repository that
is not genuinely maintainer-curated would silently change this trust
posture — treat additions to that tuple with the same care as adding a new
built-in.

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
  `skill.browse_installed` / `agent.skills_updated`, all content-free.

## API

Under `/api/v1/workspaces/{workspace_id}`:

- `GET /skills`, `GET /skills/{id}` — viewer+; list returns summaries,
  detail includes body and files.
- `POST /skills`, `PATCH /skills/{id}`, `DELETE /skills/{id}` — admin.
- `POST /skills/install-builtins` — admin; idempotently installs whichever
  of the five shipped starters (`writing-clear-updates`,
  `code-review-checklist`, `bug-report-triage`, `meeting-notes-summary`,
  `release-notes`) are still missing, which live as real skill folders in
  `packages/skills/src/jhin_skills/builtins`. New workspaces already have
  all five from creation; this is "install missing defaults" for existing
  ones.
- `POST /skills/import`, `POST /skills/import-zip` — admin; see above.
- `GET /skills/browse`, `POST /skills/browse/install` — the browse
  gallery, see above (viewer / admin respectively).
- `GET|PUT /agents/{agent_id}/skills` — viewer reads, admin replaces the
  agent's enabled set (`{"skill_ids": [...]}`).

Outside any workspace: `GET /api/v1/skill-sources` — any authenticated
user; the hardcoded browse catalog.

## Web

The **Skills** page (primary navigation, after Apps) has two sections:
**Library** — install starters, import from GitHub or a zip, create and
edit skills, toggle and delete (imported skills carry a "review and
enable" banner) — and **Browse library**, a search box over the live
gallery with one-click Install cards (already-installed skills show
disabled, labeled "Installed"; GitHub being unreachable shows a friendly
inline error, not a crash). The agent profile's **Skills** tab lets admins
pick which library skills the agent carries, with a hint when the agent
lacks a `skills.read` grant. The wizard's Tools & Access step offers two
skills-related presets: "Skills" (`skills.read`, read-only) and "Skill
authoring" (`skills.create` + `skills.update`, elevated/approval-gated).
