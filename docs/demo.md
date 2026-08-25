# Demo walkthrough and screenshots

This walkthrough uses only in-stack fake services. No external account,
model API key, or real credential is involved, and nothing leaves the host.

## What you need

- A Linux host with Docker Engine and Compose v2, Python 3.13, `uv`, and
  `make` (macOS with Docker Desktop works for everything except CLI sandbox
  jobs; see the README note).
- A checkout of this repository.

## One-time setup

Start a development stack with `compose.dev.yaml` plus exactly one socket
mode overlay from the README quick start, then:

```bash
make migrate
make seed
```

The seed is idempotent and refuses to run if any user already exists. It
creates:

| Item | Value |
|---|---|
| Owner login | `owner@jhin.dev` / `jhin-dev-password` (development only) |
| Organization | Engineering (CTO -> Senior Software Engineer, QA Engineer) and Marketing (Marketing Director -> Blogger) |
| Model provider | "Fake Provider (dev)" pointing at the in-stack `fake-provider`, with profiles `Fake Mini` (workspace default) and `Fake Pro` |
| Connector | "Linear (fake, dev)" pointing at `fake-linear`, with read/search/metadata/comment grants for the Senior Software Engineer |
| Trigger | "Pick up new engineering tickets": WHEN a Linear issue in team ENG changes state to Todo, THEN create a task for the SWE using the `engineering_ticket` template with QA review, and comment the outcome back |

Expected duration: under five minutes after the images are built.

## The demo flow

1. Open <http://localhost:3000> and sign in with the seeded owner. You land
   on **Home** (`/home`): what needs a person, what the agents are running
   right now with their latest handoffs, your recent chats, this month's
   spend, and a team snapshot.
2. **Chats** (`/chats`): pick the Senior Software Engineer and ask for
   something small ("Summarise what you can do and which tools you have").
   The reply comes from the fake provider; the Details panel shows the
   underlying work episode, tokens, and cost.
3. **Company** (`/company`) and **Agents** (`/agents`): the org map and agent
   profiles (purpose, colleagues, what each agent can use, recent activity).
4. **Apps** (`/apps`): the seeded fake Linear connection. "Manage" opens the
   connection drawer — agent access summary, recent tool usage, the Tools
   tab, and Verify; credential rotation, webhook setup, disable, and delete
   sit under "Advanced settings". `/connectors` redirects here.
5. **Automations** (`/automations`): the seeded trigger. "Test" dry-runs it
   against a sample event with per-condition pass/fail explanations.
6. Fire the flagship event from the host (fake Linear listens on
   `127.0.0.1:8092` in the dev overlay):

   ```bash
   # Point fake Linear's webhook at the seeded connection. The seeded
   # public id and secret are fixed development values defined in
   # apps/api/src/jhin_api/seed.py; the connection page shows the URL.
   curl -X POST http://localhost:8092/_admin/webhook \
     -H 'content-type: application/json' \
     -d '{"url":"http://api:8000/api/v1/webhooks/linear/<public_id>","secret":"<secret>"}'

   # The moment: move ENG-142 from Backlog to Todo.
   curl -X POST http://localhost:8092/_admin/issues/ENG-142/transition \
     -H 'content-type: application/json' -d '{"state":"Todo"}'
   ```

7. **Activity** (`/activity`): watch the trigger create "[ENG-142] ...", the
   SWE pick it up, delegate QA, and the QA verdict come back.
8. **Attention** (`/attention`): anything waiting on you (approvals, failed
   work, chats waiting for input). Approve from here if a tool call required
   approval.
9. **Advanced** (`/advanced`): the operational screens. Tasks show the
   timeline "Started by trigger ... from linear ENG-142", Runs show tokens
   and cost, Audit shows every gateway decision.
10. `curl http://localhost:8092/_state` shows the outcome comment fake Linear
    received. Re-firing the same transition never creates a second task.

To reset, stop the stack with `make compose-down` (it removes the isolated
volumes) and repeat the setup. The seed never overwrites an existing
environment.

## Screenshot set

Screenshots are captured from this seeded state at 1440x1000 (desktop) and
390x844 (mobile), with reduced motion, a fixed locale/timezone, and local
fonts, and are reviewed by a human before they are committed under
`docs/assets/screenshots/`. They must contain no credential, host path, or
real account data. The public set:

| File | Route | Shows |
|---|---|---|
| `chats-desktop.png` | `/chats/<id>` | a conversation with inline handoff/review cards and the Details panel |
| `company-desktop.png` | `/company` | organization outline and org map |
| `agent-profile-desktop.png` | `/agents/<id>` | agent purpose, colleagues, tools, recent activity |
| `agent-wizard-desktop.png` | `/agents/new` | template selection and configuration flow |
| `automation-desktop.png` | `/automations` | the seeded trigger and its dry-run explanation |
| `activity-desktop.png` | `/activity` | delegation, QA review, and outcome cards for ENG-142 |
| `attention-desktop.png` | `/attention` | pending approval and tool-access surface |
| `apps-desktop.png` | `/apps` | connector access summary for the fake Linear connection |
| `chats-mobile.png` | `/chats` | mobile layout |

The screenshot assets are produced and reviewed as part of the release
approval (Phase 11 Project C); until they are committed, this table is the
contract for what the set contains.
