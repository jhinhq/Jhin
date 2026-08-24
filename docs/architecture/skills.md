# Agent Skills

Skills give agents reusable, operator-curated instruction packs — "how we
write release notes here", "our code review checklist" — that agents
discover by name and read on demand while they work.

This document is the implementation contract for the skills package, the
API, the gateway tool, the prompt block, and the web app. Code lives in
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
  policy) — a text file never grants capability.
- Frontmatter parsing is a minimal, bounded reader of the flat
  `key: value` mappings the format uses (flow and block lists included),
  capped at 8 KB. No YAML engine, no tags, no anchors.

Validation (`jhin_skills.parser`): `name` is a slug (lowercase letters,
digits, hyphens, ≤ 64 chars; defaults to the folder name), `description`
is required and ≤ 500 chars, the body is ≤ 64 KB, each reference file is
≤ 64 KB, a skill totals ≤ 256 KB across ≤ 20 files, and obviously
credential-like content (private keys, provider API keys, tokens) is
rejected outright.

## Data model (migration `0022`, down revision `0021`)

### `skill`

| column | type | notes |
| --- | --- | --- |
| `id` | uuid pk | UUIDv7 |
| `workspace_id` | uuid fk workspace (cascade) | indexed |
| `name` | varchar(64) | slug; unique per workspace |
| `description` | varchar(500) | shown to agents in the prompt |
| `content` | text | the SKILL.md markdown body (frontmatter stripped) |
| `files_json` | json list | `[{"path", "content"}, ...]` reference files |
| `source` | varchar(16) | `built_in` / `imported` / `custom` |
| `source_url` | varchar(500) | provenance for imports (`""` otherwise) |
| `enabled` | boolean | workspace-level switch |
| `version` | integer | bumped on content/file edits |
| `created_at`, `updated_at` | timestamptz | |

### `agent_skill`

The per-agent enablement join table (`workspace_id`, `agent_id`,
`skill_id`, unique on agent + skill). Deny-by-default: no row means the
skill does not appear in that agent's prompt.

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

## Security model

- **Admin-curated**: creating, editing, enabling, deleting, importing, and
  per-agent enablement are admin-only (viewers read the library; the API
  additionally enforces admin in the service layer). Skill content is
  therefore operator-approved instruction text — it enters the prompt as
  ordinary curated context, not labeled untrusted.
- **Import review**: `POST /skills/import` (GitHub `owner/repo[/path]`
  fetched via the `codeload.github.com` zip over HTTPS, redirect-free,
  ≤ 5 MB) and `POST /skills/import-zip` (multipart upload) create skills
  as `enabled=false` "proposed" entries. Nothing reaches an agent until an
  admin reviews and enables each one.
- **Size caps everywhere**: 5 MB per archive, 64 KB per document, 256 KB
  per skill, 50 skills per bundle, bounded frontmatter, bounded tool
  output.
- **Secret screening**: skill bodies and files are scanned for obvious
  credential patterns on create, update, and import; matches are rejected
  (never stored-then-redacted).
- **Audit**: `skill.created` / `skill.updated` / `skill.enabled` /
  `skill.disabled` / `skill.deleted` / `skill.builtins_installed` /
  `skill.imported` / `agent.skills_updated`, all content-free.

## API

Under `/api/v1/workspaces/{workspace_id}`:

- `GET /skills`, `GET /skills/{id}` — viewer+; list returns summaries,
  detail includes body and files.
- `POST /skills`, `PATCH /skills/{id}`, `DELETE /skills/{id}` — admin.
- `POST /skills/install-builtins` — admin; idempotently installs the five
  shipped starters (`writing-clear-updates`, `code-review-checklist`,
  `bug-report-triage`, `meeting-notes-summary`, `release-notes`), which
  live as real skill folders in `packages/skills/src/jhin_skills/builtins`.
- `POST /skills/import`, `POST /skills/import-zip` — admin; see above.
- `GET|PUT /agents/{agent_id}/skills` — viewer reads, admin replaces the
  agent's enabled set (`{"skill_ids": [...]}`).

## Web

The **Skills** page (primary navigation, after Apps) manages the library:
install starters, import from GitHub or a zip, create and edit skills,
toggle and delete. Imported skills carry a "review and enable" banner. The
agent profile gains a **Skills** tab where admins pick which library
skills the agent carries, with a hint when the agent lacks a `skills.read`
grant.
