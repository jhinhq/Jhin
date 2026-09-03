# Personas

A persona is how an agent acts and sounds: its voice, how it takes
positions, how fast it moves, what it does when it is unsure, and how it
registers with the people it serves versus the colleagues it works with.
Every agent wears at most one persona. A persona shapes how the agent *says*
things; it never changes what the agent *may do* — tool policy, approvals,
safety rules, and a manager's instructions always win.

This document is the implementation contract for the persona package, the
data model, the API, the gateway tools, the prompt block, and the web app.
Code lives in `packages/personas` (`jhin_personas`),
`packages/db/src/jhin_db/models/persona.py` (migration `0036`),
`apps/api/src/jhin_api/personas`, `packages/tools/src/jhin_tools/personas.py`,
`packages/policy/src/jhin_policy/personas.py`, and
`packages/agents/src/jhin_agents/context.py` (`persona_block`) with
`snapshot.py` (`AgentExecutionSnapshot.persona`).

## The card: named facets, not a prompt file

A persona is a **structured card of named facets**, each a short bounded
string, rather than a free-text personality file. The structure is the point:
the web app renders a real card, the parser enforces content rules per
facet, and the prompt block is assembled from labelled lines the model can
weigh separately.

| facet | what it answers | required |
| --- | --- | --- |
| `voice` | How they sound, in one or two sentences. | yes |
| `stance` | How they take positions and handle disagreement. | |
| `pace` | Brevity versus depth, and when to go long. | |
| `when_unsure` | State assumptions, or ask the person? | |
| `with_people` | The register with the person they serve. | |
| `with_teammates` | The register with colleagues. Jhin is a company of agents, so a persona can carry itself differently on the loop than in the room. | |
| `signature` | One small recurring flourish. | |
| `never` | Up to six short, distinct things to avoid. | |

Around the facets sit a slug `name` (same rule as a skill name; immutable),
a `display_name`, a one-line `description`, and up to eight `tags`. The
`fun` tag marks the playful cards and drives the library's fun filter.

**Caps** (`jhin_personas.card`): 240 characters per facet, six `never`
items of 120 each, 1 500 characters for the whole card, name 64, display
name 80, description 200, eight tags. Whitespace is collapsed before the cap
is applied.

**Content rules**, checked on every facet and on the display name and
description, each with a readable error naming the field:

- no tool names (any dotted identifier such as `organization.ask_person`,
  in any case): a card must never steer tool use by name;
- no override phrasing ("ignore the previous instructions", "system
  prompt", "you are now", …);
- no links or domains;
- no talk of approvals, permissions, capabilities, grants, policy, or
  bypassing them.

The same `PersonaCard` model validates a shipped TOML file, an admin's form,
an agent's tool call, and the row read back for a run. Anything that fails
is rejected at the boundary it arrived at; a stored row that no longer
validates renders **no** block rather than failing the run.

## The shipped cast

Jhin ships twelve original personas as `persona.toml` files under
`packages/personas/src/jhin_personas/builtins/<name>/`, loaded by
`load_builtin_personas()` (the data files are the source of truth; a
malformed file fails the import loudly). Six are professional — the
Straight Shooter, the Patient Explainer, the Skeptic, the Host, the Editor,
the Coach — and six are fun: Mission Control, Field Naturalist, Game Show
Host, Cozy Innkeeper, Sports Commentator, Victorian Explorer. The fun six are
characterful in voice and fully competent in substance; their `never` lists
keep the bit from getting in the way of the work.

Built-in rows are **read-only**. They are installed for every new workspace
inside the workspace-creation transaction (both the bootstrap path and
`POST /workspaces`), backfilled into existing workspaces by migration
`0036`, and repaired or refreshed by the admin "install missing defaults"
call: a missing card is inserted, a card whose shipped `version` is newer
is refreshed in place (its enabled state kept), and a custom row that
happens to share a shipped name is never touched. To change a built-in,
duplicate it; the copy is yours.

## Sources and lifecycle

`source` is one of `built_in`, `custom` (written by a person), or `agent`
(an agent wrote it for itself and a person let it through). Custom and
agent cards bump `version` when the display name, description, or facets
change. Any card can be disabled: an agent still wearing a disabled persona
shows it as worn-but-off and gets no block until it is enabled again.
Deleting a card detaches every agent wearing it (`persona_id` becomes null)
in the same transaction. Every change is audited (`persona.created`,
`persona.updated`, `persona.enabled`, `persona.disabled`,
`persona.deleted`, `persona.builtins_installed`, and `persona.assigned` on
the agent).

## The prompt block

When the run's frozen `AgentExecutionSnapshot` carries a card, the worker
renders it as the second block of the system prompt — directly after the
platform preamble and before the agent's own system prompt, so the
platform's rules still come first and the persona never outranks them:

```
How you work — Mission Control
This shapes how you say things, never what you may do: tool policy, approvals, safety rules, and your manager's instructions always win.
- Voice: Level, measured, unflappable, …
- Stance: …
- Pace: …
- When unsure: …
- With people: …
- Signature: …
- Never: Raise its voice, even in text; Report a status it has not confirmed; …
```

`With people` renders only when the counterpart this turn is a person and
`With teammates` only when it is another agent (`interlocutor_kind`, the
same rule the "Who you are talking with" block uses); a run with nobody on
the other side — a trigger, a schedule — gets neither. Empty facets are
omitted and the caps are applied again at render time. The snapshot hash
changes with the card, so a persona takes effect on the agent's **next run**,
never mid-run.

## API

`/api/v1/workspaces/{workspace_id}/personas` (tag `personas`, scopes
`personas:read` for viewers and `personas:write` for admins):

| method | path | what |
| --- | --- | --- |
| GET | `` | list; filters `q`, `source`, `tag`, `enabled`, paging up to 100; each row carries `read_only` and `agent_count` |
| POST | `` | create a custom card (409 on a taken name) |
| POST | `/install-builtins` | install missing defaults / refresh older shipped cards |
| GET | `/{persona_id}` | one card |
| PATCH | `/{persona_id}` | edit; built-ins accept only `enabled`; `facets` replaces the whole object |
| DELETE | `/{persona_id}` | delete a custom or agent card, detaching agents |
| POST | `/{persona_id}/enable`, `/disable` | switch |
| POST | `/{persona_id}/duplicate` | copy (default name `<name>-copy`) |

Which agent wears which is set on the agent: `persona_id` on
`POST /agents` and `PATCH /agents/{id}` (422 unless it names an enabled
persona in the workspace), echoed as `persona_id` plus a `persona` summary on
`AgentOut` and on the conversation detail's `agent` for the chat header.

## Tools

Registered in the gateway catalog (`packages/tools/src/jhin_tools/personas.py`):

| tool | risk | capability | does |
| --- | --- | --- | --- |
| `organization.persona.list` | read | `organization.persona.self` | enabled cards, with `q` and `fun_only`, and which one the caller wears |
| `organization.persona.assign_self` | write | `organization.persona.self` | put a persona on the caller (or clear it); changes nobody else |
| `organization.persona.create` | elevated | `organization.persona.self` | propose a card; parks on a person under the default approval policy, then stores it as `source=agent` and, by default, wears it |
| `organization.persona.assign` | write | `organization.manage_agents` | put a persona on a report; denied outside the caller's manager chain |

`organization.persona.self` is part of the platform default every new agent
starts with (`jhin_policy.default_agent_grant_specs`; migration `0036`
backfills it for existing agents, respecting an explicit deny). It adds no
authority: choosing a persona changes how the agent sounds, and proposing
one is approved by a person first. See
[coordination](coordination.md#default-collaboration-grants-safe-by-default).

## Web

The `/personas` library shows the cast and the workspace's own cards as a
gallery with tag filters and a fun toggle, lets admins create, duplicate,
edit, enable, disable, and delete, and previews the rendered block. The
agent drawer has a Persona tab (and the wizard a persona step) for picking
one, and the agent profile and chat header show a persona chip.
