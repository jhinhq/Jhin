# Starter templates

Jhin ships two kinds of starter content: agent templates in the creation
wizard and a seeded example organization for development and demos.

## Agent templates (wizard)

`/agents/new` offers "Start from a template". Each template prefills the
role title and instructions; everything remains editable before the agent
is created. The current catalog (`apps/web/lib/wizard.ts`):

| Template id | Name | Role title |
|---|---|---|
| `cto` | CTO | Chief Technology Officer |
| `swe` | Software Engineer | Senior Software Engineer |
| `qa` | QA Engineer | QA Engineer |
| `devops` | DevOps | DevOps Engineer |
| `marketing-director` | Marketing Director | Marketing Director |
| `blogger` | Blogger | Content Writer |
| `seo` | SEO Specialist | SEO Specialist |
| `generic` | Generic Assistant | Assistant |

Templates do not grant tools or connectors. Capabilities are added in wizard
step 5 (Tools & connections) and step 6 (Autonomy & approvals), or later
from the agent's Tools & Access tab, and they are always least-privilege:
deny by default, scoped to exact connector dimensions.

## Seeded organization (`make seed`)

With a development stack running and migrations applied, `make seed`
(`jhin-seed-dev`) creates a reference company you can copy:

```text
Engineering (manager: CTO)
  CTO ................... "You lead the engineering organization. Break work down and delegate."
  Senior Software Engineer (reports to CTO) ... "You implement well-tested, production-quality software."
  QA Engineer (reports to CTO) ................ "You verify changes and hunt for regressions before release."
Marketing (manager: Marketing Director)
  Marketing Director .... "You own the marketing strategy and delegate content work."
  Blogger (reports to Marketing Director) ..... "You write clear, useful blog posts."
```

It also wires:

- the fake model provider and two priced profiles so agents run with no
  API keys;
- delegation defaults: the CTO may delegate to subordinates, the SWE's
  delegation is pinned to QA, QA holds sandbox read/test and GitHub read
  grants, and `report_result` closes the chain;
- the fake Linear connection with SWE grants and the enabled trigger
  "Pick up new engineering tickets" that selects the `engineering_ticket`
  workflow template with QA review and comment-back.

The seed is idempotent and refuses to run when any user exists, so it never
overwrites a real environment.

## Adapting the templates to your company

1. **Create the org shape first.** Under `/company` (or Advanced >
   Organization) create teams and set each team's manager agent.
   Reporting relationships constrain delegation scope; they do not grant
   delegation authority by themselves.
2. **Create agents from templates**, then replace the instructions with
   your real responsibilities, escalation rules, and definition of done.
3. **Attach a model profile.** Agents use the workspace default profile
   unless you pick a custom one (wizard step 4 or the agent's Model tab).
   Add a real provider under Advanced > Models; keys go into the encrypted
   secret store.
4. **Grant tools narrowly.** Start with read/search scopes on one connection
   and widen only when a workflow needs it. Keep approval policies on for
   writes (`Autonomy & approvals`).
5. **Enable delegation deliberately.** Grant `organization.delegate` with
   the exact target scope to managers who should hand work down.
6. **Automate with a trigger.** Copy the seeded trigger: WHEN a connector
   event arrives, IF your filter matches (team, state transition), THEN
   create a task for the right agent; pick the `engineering_ticket`
   template to get implementer/QA routing.

## Roadmap

The Phase 11 design moves these templates into a versioned catalog
(`starter_templates.v1.json`) served by the API with stable ids, organization
templates (`engineering-team`, `marketing-team`), and least-privilege
capability presets, so the wizard and the seed share one source. Until that
lands, the wizard list above and `apps/api/src/jhin_api/seed.py` are the two
places to edit.
