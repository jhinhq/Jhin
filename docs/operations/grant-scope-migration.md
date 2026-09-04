# Upgrading: grants that now have to name a connection, a branch, and a base

Three tools started *requiring* scope dimensions that used to be optional:

| Capability | Now requires |
| --- | --- |
| `cli.repository.checkout` | `connection_id`, `repository` |
| `cli.repository.push` | `connection_id`, `repository`, `branch` |
| `github.pull_request.create` | `connection_id`, `repository`, `base` |

"Required" means the evaluator denies the call unless at least one **allow**
grant carries every one of those keys. It is not enough for the *call* to name
them — the grant has to.

## Why

A grant only constrains the keys it mentions. `scope_matches` walks the granted
scope, not the requested one, so a push grant of
`{connection_id, repository}` matched a request to push **any** branch,
including `main`. The in-sandbox script refused a handful of names
(`main`, `master`, `HEAD`, and the branch the checkout was cut from), and that
was the entire limit. The same shape let a pull request grant target any base
branch in the repository.

An unstated dimension is an unlimited one. Making these keys required means a
grant can no longer be silently broad: whoever writes it has to say `agent/*`
or `*`, and the next person to read it can see which.

## What the upgrade does to grants you already have

Migration `0039` runs automatically and **restates** affected grants. It never
widens or narrows what an agent may do:

- an absent `repository`, `branch` or `base` already meant "any", so it is
  written down as `"*"`;
- an absent `connection_id` is filled in **only** when the workspace has exactly
  one connection of the right type (`cli` for the sandbox tools, `github` for
  pull requests). That is a narrowing — from "whichever connection the call
  named" to the one that exists — and it is the safe direction.

### Grants that never named the capability

`capability` is a pattern, not only a name: `cli.*` and `*` authorise
`cli.repository.push` exactly the way `cli.repository.push` does, so a wildcard
grant loses the same authority and needs the same restating. It cannot be
restated in place — adding `repository` to a `cli.*` grant would constrain
every other `cli` tool it covers, and a `cli.command.execute` call carries no
`repository` at all, so those calls would start being denied.

So `0039` leaves the wildcard grant exactly as it is and writes the authority
down **beside** it: one exact-capability grant per affected capability it
covers, carrying that grant's own scope plus the restated dimensions. After the
upgrade a `cli.*` grant scoped to a connection is joined by

```
cli.repository.checkout   connection_id=…, repository=*
cli.repository.push       connection_id=…, repository=*, branch=*
```

which is what the wildcard row already allowed, now visible on the Permissions
tab where it can be narrowed. Nothing is written where the agent already has a
grant of its own for that capability — in either effect — or where
`connection_id` cannot be answered for.

A wildcard grant that names a connection reaches only that connection's own
tools, and the derived rows say so: a `*` grant scoped to a **CLI** connection
gets the two `cli.*` rows above and **no** `github.pull_request.create` row.
That grant never authorised a GitHub call — scope matching compares the
`connection_id` the call carries against the one the grant names, and a GitHub
call carries a GitHub connection — so a row carrying the CLI id could only ever
sit on the Permissions tab granting nothing, and a row carrying the workspace's
GitHub connection would grant something the wildcard row never did.

### Grants the migration leaves alone

Grants the migration leaves alone are ones it cannot answer for you: a workspace
with **no** connection of that type, or with **more than one**. Those calls now
fail with

```
required_scope_missing: no grant for 'cli.repository.checkout' is scoped by
every required dimension; the closest is missing: connection_id
```

which names the key to add. Add it on the agent's **Permissions** tab (or
re-apply the **Code editing** tool preset, which writes all of them).

## What to do after upgrading

1. Open **Agents → *agent* → Permissions** for every agent with sandbox or pull
   request grants and look at the restated scopes. Every `"*"` the migration
   wrote is breadth the grant already had and nobody had to look at. Narrowing
   `branch` to `agent/*` and `base` to your default branch is the change worth
   making, and it takes one edit each.
2. Check the CLI Sandbox connection's **Repositories this sandbox may use**
   list (migration `0038` grandfathered existing connections to `*`). It is the
   per-instance bound underneath every agent's grants.
3. Agents created through the wizard need nothing: the Code-editing preset has
   always written `branch: agent/*` and `base: main`.

## Rolling back

`alembic downgrade 0038` removes only the values `0039` would have written and
the rows it would have inserted, and only where they still look untouched — a
scope you have since edited is left as you edited it.
