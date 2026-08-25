# Roles and permissions

Jhin has four workspace roles. This page is the authoritative statement of what
each one may do, and the reasoning behind the boundaries. The API enforces it
in exactly one place — `require_workspace_role` in
`apps/api/src/jhin_api/deps.py` — and the boundaries are pinned by
`apps/api/tests/test_access_control.py`.

Plan reference: implementation plan §20.2.

## The four roles in one line each

| Role | Plain-language meaning |
|---|---|
| **Owner** | The person who owns the company. Can do everything, including deleting the workspace and deciding who else is an owner. |
| **Admin** | Runs the company day to day: sets up agents, apps, automations, models, and budgets, and invites people. |
| **Member** | Uses the company: chats with agents, gives them work, decides approvals routed to them. Changes no configuration. |
| **Viewer** | Reads everything operational and changes nothing — including not starting work, because starting work spends money. |

Roles are ordered: `viewer < member < admin < owner`, and every higher role
inherits everything below it (`jhin_domain.role_satisfies`).

## Why viewers cannot chat

A viewer is genuinely read-only, and "read-only" here has to include *cost*.
Sending a message to an agent starts a run, and a run spends real money against
the workspace's model providers. A role that can spend the company's money is
not a read-only role. So `chats:write`, `tasks:write`, and everything that
signals a running task require member.

## The matrix

`•` = allowed, `–` = denied.

| Area | Viewer | Member | Admin | Owner | Notes |
|---|:--:|:--:|:--:|:--:|---|
| **Workspace** | | | | | |
| Read workspace, org chart, people directory | • | • | • | • | |
| Rename workspace, change timezone/settings | – | – | • | • | |
| Delete the workspace | – | – | – | • | Irreversible; owner only. |
| Count what deleting it would destroy | – | – | – | • | The confirmation dialog's inventory. |
| **People** | | | | | |
| See members and pending invitations | • | • | • | • | Emails only, never credentials. |
| Invite a viewer / member / admin | – | – | • | • | Admins may create admins — see below. |
| Invite an owner | – | – | – | • | |
| Change or remove a viewer / member | – | – | • | • | |
| Change or remove an **admin** | – | – | – | • | Admins may promote, never demote a peer. |
| Change or remove an **owner** | – | – | – | • | |
| Act on **your own** membership (leave, step down) | • | • | • | • | Subject to the last-owner rule. |
| **Agents** | | | | | |
| List and read agents, avatars, reporting lines | • | • | • | • | |
| Create, edit, delete agents | – | – | • | • | |
| Pause / resume an agent | – | – | • | • | Changes shared state for everyone. |
| Grant or revoke agent capabilities; edit autonomy policy | – | – | • | • | Plan §20.3: agent capability is separate from user role. |
| **Teams** | | | | | |
| Read teams and membership | • | • | • | • | |
| Create, edit, delete teams; move agents between them | – | – | • | • | |
| **Chats** | | | | | |
| Read conversations and messages | • | • | • | • | |
| Start a chat, send a message | – | • | • | • | Starts a run. |
| Rename / pin / archive / delete **your own** chat | – | • | • | • | |
| Rename or delete **someone else's** chat | – | – | • | • | |
| **Tasks and runs** | | | | | |
| Read tasks, trees, timelines, runs, tool calls | • | • | • | • | |
| Start, steer, pause, cancel, instruct a task | – | • | • | • | |
| **Approvals and reviews** | | | | | |
| See pending approvals and reviews | • | • | • | • | |
| Approve / reject a paused action | – | • | • | • | A member is a legitimate human in the loop. |
| Submit a review verdict; answer work requests | – | • | • | • | |
| Create / edit / delete review policies | – | – | • | • | Policy is configuration. |
| **Apps (connections)** | | | | | |
| See connections and the tools they expose | – | – | • | • | Connection metadata is close enough to credentials. |
| Connect, edit, disable, disconnect an app | – | – | • | • | |
| Rotate credentials or webhook secrets | – | – | • | • | Browser session only; no API key, ever. |
| **Automations (triggers)** | | | | | |
| List triggers and their invocation history | • | • | • | • | |
| Create, edit, test, enable, delete triggers | – | – | • | • | Testing is part of authoring. |
| **Skills** | | | | | |
| Browse the library, read a skill | • | • | • | • | |
| Install, import, edit, remove; assign to agents | – | – | • | • | |
| **Memories** | | | | | |
| Read curated memory | • | • | • | • | |
| Create, edit, pin, contest a memory | – | • | • | • | Members contribute knowledge. |
| Approve, reject, forget, de-duplicate | – | – | • | • | Curation is an editorial act. |
| **Models, budgets, and spend** | | | | | |
| See model providers and their configuration | – | – | • | • | Provider rows are credential-adjacent. |
| Add / edit / delete providers and profiles | – | – | • | • | |
| Read provider balance | – | – | • | • | Calls the provider with the workspace's key. |
| Read what the workspace has spent | • | • | • | • | Operational transparency; see below. |
| Set the monthly budget | – | – | • | • | A budget is configuration. |
| Read and write workspace secrets | – | – | • | • | Values are never returned to anyone. |
| **Audit** | | | | | |
| Read the audit log | – | – | • | • | |
| **API keys** | | | | | |
| Create a key for yourself, list keys, revoke your own | • | • | • | • | Capped by your own role — see [API keys](api-keys.md). |
| Revoke someone else's key | – | – | • | • | |
| Read the usage log | • | • | • | • | Scope of what you see depends on role. |

## Two decisions worth spelling out

### Admins may create admins, but only an owner may unmake one

An admin can invite or promote someone all the way to admin. Requiring the
owner for that would mean a workspace whose owner is asleep cannot get a second
operator, which is exactly when it needs one — and the risk is small, because
an admin creating an admin hands out no power the creator did not already have.

Taking admin *away* is the opposite shape. If an admin could demote a peer,
any single admin could demote every other admin and become the sole operator:
a takeover with extra steps. So removing or demoting an admin — or touching an
owner at all — requires the owner. Acting on your own membership is always
allowed: leaving a workspace or stepping down is not an escalation.

Implemented as `require_authority_over` (may I hand out this role?) and
`require_authority_to_modify` (may I change this existing membership?) in
`apps/api/src/jhin_api/workspaces/service.py`.

### Spend is visible to everyone; provider setup is not

The running model bill is shown company-wide, on the home dashboard, to every
role. That is deliberate: agents spend money on the company's behalf all day,
and hiding what that costs from the people directing them would make the
product feel like it had something to conceal. The figure carries no
credentials and no configuration.

Everything *around* it is admin: the provider rows (endpoints, credential
references, verification state), the live balance call — which spends an
outbound request against the workspace's own provider key — and setting the
budget. So a member can see that the company spent $42 this month and cannot
see, touch, or bill anything that produced it.

### A workspace always keeps one owner

Removing or demoting the last owner is refused with `409`, by everyone,
including that owner. A workspace with no owner has no one who can delete it,
transfer it, or manage its admins — it is permanently stuck. To step down, an
owner promotes someone else first.

## Invitations

There is no way to set another person's password in Jhin, deliberately: an
admin who types a colleague's password has seen it. Instead an admin creates an
**invitation** — a single-use link that expires (7 days by default,
`INVITATION_TTL_DAYS`) and lets the invitee choose their own credential.

* `POST /api/v1/workspaces/{id}/invitations` (admin+) mints the link and
  returns it **once**. Only `sha256(token)` is stored, exactly as with session
  tokens, so a database leak yields nothing replayable.
* Jhin has no email sender and this feature does not add one. The link is shown
  to the inviting admin to pass along out of band, clearly labelled as a
  one-time reveal.
* `GET /api/v1/invitations/{token}` is public and returns only the workspace
  name, the invited address, and the role — whoever holds the link is not a
  member yet. Lookup is by token hash: one indexed equality, so an unknown
  token costs the same as a real one.
* `POST /api/v1/invitations/{token}/accept` creates the account and the
  membership in one transaction, stamps `accepted_at` (making the link
  single-use), and signs the new person in.
* Unknown, expired, revoked, and already-accepted tokens all return the same
  `404` and the same wording.
* Re-inviting an address revokes any outstanding link for it, so a lost
  invitation cannot linger as a second live credential.
* If the address already has an account, accepting only adds the membership.
  The submitted password is ignored: joining a workspace must never double as
  a password reset.

## Enforcement

Every workspace-scoped route resolves through `require_workspace_role`, which:

1. finds the caller's membership, returning `404` (not `403`) to a non-member
   so workspace existence is not leaked;
2. for an API key, caps the effective role at the key's ceiling and checks the
   route's required scope (see [API keys](api-keys.md));
3. compares the effective role against the route's floor, returning `403` if
   it falls short.

Services then filter every query by `workspace_id`, so a mistake in a route
declaration cannot become cross-workspace data access.
