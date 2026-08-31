# Support

## Where to ask

- **Usage questions and troubleshooting:** open a
  [GitHub Discussion](https://github.com/jhinhq/Jhin/discussions) (or an
  issue if Discussions are not enabled yet). Include your Jhin version
  (`cat VERSION` or the image digest), deployment mode, host platform, and
  redacted `docker compose ps --all` / health output.
- **Bugs and feature requests:** use the issue forms at
  <https://github.com/jhinhq/Jhin/issues/new/choose>.
- **Security vulnerabilities:** never post publicly. Use the private advisory
  route described in [SECURITY.md](SECURITY.md).

## Before asking

1. Read the [deployment guide](docs/deployment.md), especially
   "Troubleshooting".
2. Check `curl -s http://localhost:8000/api/v1/health/ready` and
   `docker compose logs <service>` for the failing component.
3. Confirm you started the stack with exactly one Docker-socket overlay
   (rootful or rootless) as documented in the README quick start.

Never paste credentials, master keys, webhook secrets, or session cookies into
any public channel.

## Code of conduct reports

Conduct concerns are handled confidentially by the project maintainers.
Until a dedicated monitored mailbox is published here by the repository
owner, contact a maintainer listed in `.github/CODEOWNERS` directly through a
private GitHub message or report through the private advisory form with the
title prefix `[conduct]`. The repository owner must replace this paragraph
with the monitored confidential address before the first public release
(Phase 11 owner gate).
