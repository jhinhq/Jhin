# Changelog

All notable changes to Jhin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Major version zero means public interfaces may still change between minor
versions.

## [Unreleased]

## [0.1.0] - Unreleased

First public, self-hostable release candidate. Everything below was built
across implementation phases 1-10 of `docs/implementation-plan.md`; Phase 11
adds the open-source release artifacts.

### Added

- **Platform core (phases 1-3):** Docker Compose stack with PostgreSQL as
  system of record, NATS JetStream as event transport, and Temporal as the
  durable workflow authority; FastAPI control plane with cookie sessions,
  CSRF protection, workspaces, roles, and audit trail; first-run `/setup`
  onboarding; envelope-encrypted secret store (AES-256-GCM, per-secret DEKs,
  file-backed master key) with log redaction; model providers and priced
  model profiles; durable `AgentTaskWorkflow` agent runs with token and cost
  accounting.
- **Tool gateway and approvals (phase 4):** capability registry, tool
  definitions, per-agent grants with scoped dimensions, deny-by-default
  policy evaluation, approval policies, an approvals inbox, sanitized and
  audited tool calls.
- **Connectors (phases 5, 6, 9):** connector SDK (manifest, tools, schemas,
  webhooks) with GitHub, Linear, Vercel, Supabase (Management API and
  bounded SQL), and a CLI connector that runs jobs in ephemeral,
  non-root, read-only sandbox containers through the internal
  `sandbox-runner`; outbound endpoint policy with operator allowlists;
  fake GitHub, Linear, Vercel, Supabase, and OpenAI-compatible services for
  credential-free development.
- **Triggers and events (phase 7):** signed webhook ingestion with delivery
  dedupe, canonical event normalization on the event worker, a WHEN/IF/THEN
  trigger builder with filter DSL, dry-run explanations, and
  `TriggeredTaskWorkflow` task creation.
- **Delegation and teams (phase 8):** hierarchical organizations, manager
  relationships, grant-scoped `organization.delegate_task`, the
  `engineering_ticket` workflow template with implementer/QA routing and
  bounded fix-retest loops, per-agent concurrency limits, and queued-state
  visibility.
- **Production operations (phase 10):** a dedicated deterministic
  `tool-worker` on its own Temporal task queue that owns authorization,
  secret resolution, connector effects, and audit; the agent worker owns
  model reasoning only; rootful and rootless Docker-socket modes for
  `sandbox-runner` with fail-closed identity checks; protected health
  endpoints; structured JSON logging, OpenTelemetry traces and metrics;
  in-flight Phase 9 to Phase 10 workflow upgrade compatibility; the Phase 10
  live harness and regression suites.
- **Chat-first experience:** persistent, named conversations with every
  agent (`/chats`), a company activity feed (`/activity`), an Attention
  inbox (`/attention`), Agents and Company directories with profiles and an
  org map (`/agents`, `/company`), Automations and Apps views over triggers
  and connectors (`/automations`, `/apps`), and an Advanced area keeping
  every operational screen (`/advanced`).
- **Memory:** curated long-term memory per agent with candidate extraction,
  policy-based redaction of secrets, and review before promotion.
- **Avatars and media:** agent avatars with an image pipeline and
  multipart upload.
- **Coordination and oversight:** review policies, handoff/review/approval
  cards inline in chats, escalation visibility, and task lineage trees.
- **Open-source release (phase 11):** Apache-2.0 license metadata,
  community files (`CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `SUPPORT.md`), issue and pull-request templates, CODEOWNERS, Dependabot,
  CI/E2E/Security/Release workflows, multi-arch GHCR image matrix with SBOM,
  provenance, Cosign signing, and Trivy scanning, the release Compose bundle
  under `deploy/`, the documentation set under `docs/`, and
  `scripts/release_preflight.py`.

### Security

- Secrets are never returned after creation, never logged, and never stored
  in `.env`; the master key is file-mounted only into services that decrypt.
- Only `sandbox-runner` can reach the Docker socket, always as a non-root
  identity; job containers never receive it.
- Production Compose publishes only the web and API ports; PostgreSQL,
  NATS, Temporal, and `sandbox-runner` stay on internal networks.

[Unreleased]: https://github.com/Teachmetech/Jhin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Teachmetech/Jhin/releases/tag/v0.1.0
