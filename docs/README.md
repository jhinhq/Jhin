# Jhin documentation

Start here if you are deploying, evaluating, or contributing to Jhin.

## Operators

| Document | What it covers |
|---|---|
| [Deployment guide](deployment.md) | Production deployment contract: release bundle, reverse proxy and TLS, secrets and the master key, Docker-socket modes, sizing, backups and restore, upgrades and migrations, health endpoints, telemetry, troubleshooting |
| [Demo walkthrough](demo.md) | The seeded, credential-free demo flow and the screenshot list |
| [Starter templates](templates.md) | Agent and organization templates the seed provides and how to adapt them |
| [Release bundle](../deploy/README.md) | Pull-based Compose manifest using published GHCR images |

## Architecture

[Architecture index](architecture/README.md) maps every first-party service
and trust boundary to its authoritative document:

- [Roles and permissions](architecture/rbac.md)
- [API keys](architecture/api-keys.md)
- [API versioning and compatibility](architecture/api-versioning.md)
- [Conversations and Company Activity](architecture/conversations.md)
- [Coordination and Oversight](architecture/coordination.md)
- [Delegation and Teams](architecture/delegation-and-teams.md)
- [Connector architecture](architecture/connectors.md)
- [Vercel and Supabase connectors](architecture/vercel-and-supabase.md)
- [Events and triggers](architecture/events.md)
- [Sandboxing](architecture/sandboxing.md)
- [Pointing an agent at a real repository](operations/github-token-setup.md)
- [Upgrading: grants that now name a connection, branch and base](operations/grant-scope-migration.md)
- [Giving an agent an app: bundles, the setup dialog, `jhin-admin agent`](operations/agent-access.md)
- [Deterministic tool-worker boundary](architecture/tool-worker-boundary.md)
- [Curated long-term memory](architecture/memory.md)
- [Agent Skills](architecture/skills.md)
- [Personas](architecture/personas.md)
- [Models, pricing, and balance](architecture/models.md)
- [Agent avatars and media](architecture/media.md)
- [ADR-004: agent graph](adr/ADR-004-agent-graph-langgraph.md)

## Contributors

- [Contributing guide](../CONTRIBUTING.md): setup, gates, conventions,
  boundaries, adding a connector
- [Implementation plan](implementation-plan.md): the authoritative plan and
  phase checklists
- `superpowers/specs/` and `superpowers/plans/`: approved designs and the
  implementation plans derived from them
- [Security policy](../SECURITY.md), [Support](../SUPPORT.md),
  [Code of conduct](../CODE_OF_CONDUCT.md), [Changelog](../CHANGELOG.md)
