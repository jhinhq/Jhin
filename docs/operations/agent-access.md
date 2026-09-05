# Giving an agent an app

"It doesn't make sense that the software engineer doesn't have access to
GitHub. Also adding access is complicated." This page is the answer: one
action per app, in the browser or on the console, that writes every grant the
agent needs, creates the sandbox Code editing runs in, and refuses — by
sentence — anything the tool gateway would deny anyway.

## What a capability bundle is

A bundle is a named set of tools with the fixed parts of their scope already
decided (`branch: agent/*`, `path: *`). Turning one on for an agent fills in
the parts only your workspace can answer — which connection, which
repositories — and writes ordinary `agent_capability_grant` rows through the
same service the Capability grants list uses. Nothing about authorization
changes: the gateway still decides every call from the live rows, and a bundle
row and a hand-made row are the same kind of row with the same audit trail.

| Bundle | Gives | Needs |
| --- | --- | --- |
| `github-read` — GitHub (read) | repositories, branches, files, issues, pull requests, checks and workflow runs, read only | a GitHub connection |
| `code-editing` — Code editing | check out, browse, search, read, edit, run tests, push `agent/*` branches (always with approval), read GitHub, open pull requests | a GitHub connection; the CLI Sandbox connection is created for you |
| `web-access` — Web search & browsing | search the web and read public pages | a Web connection |
| `collaboration`, `team-building`, `skills`, `skill-authoring` | organization and skills tools | nothing to connect |

`GET /api/v1/workspaces/{id}/tools/bundles` lists them with a readiness
verdict for your workspace; `GET .../agents/{id}/bundles` says whether each is
`on`, `partial` or `off` for one agent and which of its grants cannot work as
written.

## In the browser

**Apps → the connection → Give to an agent…** picks the agent and lands on
its Tools & Access tab with the setup dialog open on that connection. Or start
from the agent: **Agents → the agent → Edit → Tools & Access** and click the
capability tile.

The dialog shows every step, pre-filled when the answer is unambiguous:

1. **GitHub** (or the app's connection) — which connection the calls go
   through. An OAuth connection acts with the authorizing person's
   permissions, and the dialog says whose.
2. **Sandbox** (Code editing only) — create a CLI Sandbox connection now, or
   use one that already borrows this GitHub connection's credential. The
   sandbox's *Repositories this sandbox may use* list is the outer limit under
   every agent's grants; `*` means every repository the token can reach.
3. **Repositories** — every repository the sandbox allows, or a list. An
   entry outside the sandbox's list is refused here, not discovered later.
   Under *Advanced*, the pull request base branch pattern (default `*`).
4. **Review** — what the agent will be able to do, the exact rows and rules
   this writes (a dry run against the server), and any warnings: an explicit
   deny that still wins, or a wildcard grant that also covers these tools.

Turning a bundle off previews what goes, names any rows you added by hand for
the same capabilities, and leaves approval rules alone.

## On the console

`jhin-admin agent …` drives the same service. `--workspace` may be omitted
when the install has one workspace.

```bash
jhin-admin agent list [--workspace <slug|id>] [--json]
jhin-admin agent access --agent <name|slug|id> [--workspace <slug|id>] [--json]
jhin-admin agent grant  --agent <name|slug|id> [--workspace <slug|id>]
                        (--bundle <id> | --app <connector_type> | --capability <cap> [--scope key=value ...])
                        [--github <name|id>] [--sandbox <name|id>] [--create-sandbox] [--sandbox-name NAME]
                        [--repositories a/b,c/d | '*'] [--base PATTERN] [--effect allow|deny]
                        [--as <email>] [--dry-run] [--yes] [--json]
jhin-admin agent revoke --agent <name|slug|id> [--workspace <slug|id>] (--bundle <id> | --grant <id>) [--yes] [--json]
```

- `--app github` is `--bundle github-read`, `--app web` is `web-access`,
  `--app cli` is `code-editing`.
- `--capability` writes one grant through the same validation as
  `POST /grants`, run before the confirmation and before a dry run: a
  capability no agent may hold, a row the evaluator would refuse on every
  call, a repository outside the pinned sandbox's allow-list, a branch the
  push tool refuses — each is the API's own sentence, and nothing is written.
  Required scope keys are never guessed: pass them with `--scope`, or the
  command tells you which one is missing.
- `--as <email>` names the admin or owner the audit trail records as acting;
  otherwise the workspace's owner. Every row carries `actor_type=system` and
  `{"cli": "jhin-admin agent grant"}`.
- `--create-sandbox` needs the master key (`docker compose exec api …` has
  it).
- `agent access` prints the bundle states, every grant with its connection
  and status (`ok` or `needs attention: …`), the approval rules, the tools
  the definition catalog would offer the agent, and the count of dangling
  grants.

### Every refusal, in its own words

| Situation | Sentence |
| --- | --- |
| more than one workspace, no `--workspace` | `More than one workspace exists; pass --workspace (`jhin-admin workspace list` shows them).` |
| unknown agent | `No agent matches '{x}' in {workspace}. `jhin-admin agent list` shows them.` |
| two agents with that name | `Two agents in {workspace} are called '{x}': {id1}, {id2}. Use the id.` |
| no bundle for an app | `There is no capability bundle for '{type}'; use --capability.` |
| required scope key missing | `` `{cap}` needs `{key}` in its scope; pass --scope {key}=... (for example {example}). `` |
| no connection of the type | `{workspace} has no active {Connector} connection. Connect one under Apps first.` |
| several connections | `More than one {Connector} connection is active: pass --github <name|id> (choices: {names}).` |
| no sandbox for this GitHub connection | `No CLI Sandbox connection uses '{github}'. Pass --create-sandbox to make one pointing at it (repositories: {list}), or --sandbox <name|id>.` |
| a sandbox already uses it | `A CLI Sandbox connection '{name}' already uses '{github}' for repository jobs; pick it under connections.cli instead of creating another.` |
| sandbox on another GitHub connection | `'{sandbox}' uses '{other}' for repository jobs, not '{github}'. Pick a sandbox that uses this connection, or change its GitHub connection under Apps first.` |
| repository outside the sandbox list (`--bundle`, and `--capability` alike) | `'{sandbox}' allows only: {list} — '{entry}' is outside it. Add it to the sandbox's allowed repositories under Apps, or grant only what the sandbox allows.` |
| malformed repository | `'{entry}' is not a repository: use owner/name, owner/*, or * for every repository.` |
| more than 50 repositories | `At most 50 repositories can be granted at once.` |
| `--capability` in a namespace no agent may hold | `capabilities in this namespace can never be granted to agents` |
| `--capability` that is not a name or pattern | `not a valid dotted capability name or pattern` |
| `--capability cli.repository.push` with `branch=main` (or `master`, `HEAD`) | `branch 'main' is refused on every push: the sandbox never pushes to main, master or HEAD. Use a pattern such as agent/*.` |
| malformed base | `base must be a branch name or pattern such as main or release/*.` |
| no master key | `JHIN_MASTER_KEY is not available in this shell; inside the compose stack `docker compose exec api jhin-admin ...` brings it with it.` |
| `--as` is not an admin | `{email} is not an admin or owner of {workspace}.` |
| a tool the workspace does not offer | `This workspace does not offer: {tools}.` |

### The live example

The operator's instance had a GitHub connection and a Senior Software
Engineer with one hand-made `github.repository.read` grant pinned to it, and
no CLI Sandbox connection at all:

```bash
docker compose -f compose.yaml -f compose.desktop.yaml exec -T api jhin-admin workspace list
docker compose -f compose.yaml -f compose.desktop.yaml exec -T api jhin-admin agent grant \
  --agent "Senior Software Engineer" --bundle code-editing --create-sandbox --repositories "*" --yes --json
docker compose -f compose.yaml -f compose.desktop.yaml exec -T api jhin-admin agent access --agent "Senior Software Engineer"
```

The first command names the one workspace; the second creates `Sandbox for
GitHub` (`default_network: none`, `git_connection_id` = the GitHub connection,
`allowed_repositories: ["*"]`), writes the eleven Code editing rows and the
`cli.repository.push → approval` rule, and prints them; the third shows Code
editing `on`, GitHub (read) `partial` (the issue, check and workflow-run reads
are not part of Code editing), the grants with their problems, and the tools
the agent would be offered.

## What the agent sees

Every reasoning step records the tool names it was offered as a run event,
`agent.step.tools_offered`, shown in the task timeline as **Tools offered**.
It is the durable answer to "was the model even shown the tool?" — the
manifest lists calls made, this lists tools offered.

On a chat turn, the previous run of the same agent in the same conversation
is compared with this turn's list, and when they differ the prompt carries:

> Your tools changed since your last reply in this conversation. Added: …
> Removed: …. Do not rely on anything you said about your tools before this
> turn.

The platform preamble (version 12) tells agents that the tool list is the
truth about what they can use now, to look at it before saying a tool is
missing, and never to report a block they did not observe — when a call is
denied, the result carries an error code and a reason, and that is what the
agent relays.

## Grants that cannot work

Every grant the API returns carries `problems`: sentences describing why the
row would be refused on every call — a missing required scope key, a
connection that no longer exists, a malformed repository, a repository the
pinned sandbox's allow-list does not cover, a branch the push tool refuses.
`POST /grants` refuses the kinds that can never come alive (422) and accepts
the two that can: a capability the catalog does not know yet (MCP servers
register tools after the connection exists) and a connection that is merely
disabled or waiting to be reconnected. A hand-made row is held to the same
width the bundle is: `repository: *` against a sandbox that allows only
`octo/a` is refused with the planner's sentence, whichever way it is written. Deleting a connection revokes every grant pinned
to it, each audited with `reason: connection.deleted`; disabling revokes
nothing. A grant pinned to a connection that is not active is not advertised
to the model at all.
