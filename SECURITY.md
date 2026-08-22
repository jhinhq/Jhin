# Security Policy

Jhin runs autonomous agents that hold credentials for external systems and
execute code in sandboxes, so we treat security reports as the highest
priority work in the project.

## Reporting a vulnerability

**Please do not open a public issue, discussion, or pull request for a
security problem.**

Report privately through GitHub's private vulnerability reporting:

<https://github.com/Teachmetech/Jhin/security/advisories/new>

Treat the following classes as confidential until a coordinated fix ships:

- secret exposure (master key, stored credentials, session cookies, webhook
  secrets, model API keys) through the API, UI, logs, audit records, or
  Temporal/NATS payloads;
- sandbox escape, Docker-socket access from a job, or privilege escalation
  in `sandbox-runner`;
- authentication or authorization bypass, CSRF, or session fixation;
- cross-workspace data access or workspace-isolation failures;
- webhook authenticity bypass (signature verification, replay, dedupe);
- tool-gateway or approval-gate bypass (an agent acting outside its grants);
- supply-chain compromise of images, dependencies, workflows, or release
  assets;
- credential leakage through connectors or outbound request policy bypass.

What to include: affected version or image digest, deployment mode
(release bundle, rootful/rootless source stack, dev overlay), reproduction
steps, and impact. **Never send live credentials, master keys, or production
data.** Redacted logs and synthetic reproductions are enough.

## Response process

| Step | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment and severity | within 7 business days |
| Fix and coordinated disclosure | agreed with the reporter; typically a patch release plus a GitHub security advisory |
| Credit | with the reporter's consent, in the advisory and `CHANGELOG.md` |

We ask reporters to allow a reasonable embargo while a fix is prepared. If a
published image is found unsafe, the affected immutable tag/digest is marked
withdrawn in the release notes and advisory, and a new patch version is
published; tags are never moved.

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` (first public line, once `v0.1.0` is tagged) | security fixes |
| unreleased `main` | best effort; not for production |

Only the most recent patch of the supported line receives fixes.

## Hardening notes for operators

- Back up the master key separately from database backups and restrict it
  to mode `0600`. Losing it makes every stored secret unrecoverable; leaking
  it exposes every stored credential.
- Run Jhin behind TLS (`COOKIE_SECURE=true`, `APP_URL=https://...`). Publish
  only the web entry point; keep the API, PostgreSQL, NATS, Temporal, and
  `sandbox-runner` off public interfaces (the base `compose.yaml` already does
  this for infrastructure).
- Never run the stack with `compose.dev.yaml` in production: it enables fake
  providers, development credentials, and the connector allowlists.
- Use exactly one Docker-socket overlay (`compose.rootful.yaml` or
  `compose.rootless.yaml`). Do not relax socket permissions, run containers
  privileged, or run Jhin as root to make sandboxing "work".
- Keep `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` and
  `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` empty unless you intentionally authorize
  credentialed traffic to those exact origins.
- Grant agents least-privilege connector scopes and keep approval policies on
  for write operations.
- Verify image signatures and SBOMs before deploying (see
  `docs/deployment.md`).

See [docs/deployment.md](docs/deployment.md) and
[docs/architecture/sandboxing.md](docs/architecture/sandboxing.md) for the
full trust-boundary description.
