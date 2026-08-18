# Phase 11 Open-Source Release Design

**Date:** 2026-08-18

**Status:** Implementation design derived from the approved authoritative plan

**Authoritative source:** `docs/implementation-plan.md`, especially sections 0, 2.8, 3,
24, 35–38, 48–49, and Phase 11

## 1. Outcome

Phase 11 turns the current repository into the first public, self-hostable Jhin
release. The release closes all sixteen Phase 11 checklist items without weakening
the security or production-readiness requirements in the authoritative plan.

The first release is `v0.1.0`. SemVer major version zero communicates that public
interfaces may still evolve; it does not lower the operational or security gates.
The release must not be described as production-ready until every criterion in
section 49 of the implementation plan has current evidence.

The work is split into five independently reviewable projects:

1. identity and community;
2. operator onboarding and documentation;
3. deterministic demo, screenshots, and starter templates;
4. supply chain and release automation;
5. publication gate.

The first four projects are repository-completable through local preparation and
verification. Once Project A's public-repository prerequisites pass, they may merge
without creating a release, registry package, or tag. The fifth consumes their
evidence and performs only explicitly authorized external release actions.

## 2. Current facts and fixed decisions

### 2.1 Name and repository identity

The final product name recorded by section 0 of the authoritative plan is **Jhin**.
`APP_NAME` remains the configurable user-facing label; package, command,
environment-variable, database, Compose, and image identifiers remain Jhin-scoped
and stable only if the owner/legal publication gate below returns a favorable written
decision.

Current repository facts:

- the configured Git remote is `https://github.com/Teachmetech/Jhin.git`;
- `github.com/Teachmetech/Jhin` is already a public repository, so pushing a branch
  or merging a change is itself a public disclosure rather than private preparation;
- the root Python workspace, Python distributions, import modules, Compose project,
  web package, and application defaults already use `jhin` or `Jhin`;
- all Python projects and `apps/web/package.json` currently declare version `0.1.0`;
- the root npm package and web package are private;
- there are no local Git tags;
- the only GitHub workflow is `.github/workflows/ci.yml`, and it builds but does not
  publish images;
- the repository contains a complete Apache-2.0 `LICENSE`;
- the configured GHCR namespace implied by the repository owner is
  `ghcr.io/teachmetech`.

Current public-name checks performed on 2026-08-18 also found that Riot Games
uses **Jhin** for a League of Legends character, the unscoped npm package
`jhin` already exists at version `0.0.6`, the unscoped PyPI project `jhin` did
not exist at the time of the check, and `jhin.ai` was registered. These facts
make the owner/legal confirmation gate material; they do not silently override
the authoritative product-name decision. The first release avoids unscoped npm
or PyPI publication and uses the repository-owned GHCR namespace.

The release uses these public coordinates:

- source repository: `github.com/Teachmetech/Jhin`;
- release tag: `v0.1.0`;
- OCI namespace: `ghcr.io/teachmetech`;
- image repositories: `jhin-web`, `jhin-api`, `jhin-workflow-worker`,
  `jhin-agent-worker`, `jhin-tool-worker`, `jhin-event-worker`,
  `jhin-sandbox-runner`, and `jhin-sandbox`.

Python and npm workspace packages are **not** published in the first release. They
remain private implementation units named `jhin-*` and imported as `jhin_*`.
Publishing them later requires a separate package-boundary and compatibility design;
the Phase 11 workflow must contain no PyPI or npm publication credentials.

### 2.2 Release approach

The selected approach is a staged full release. A source-only preview would leave
the container, SBOM, scanning, and automation requirements open. A single large
release change would couple governance, documentation, demo behavior, image
publication, and external repository settings in one hard-to-review operation.

Each project below has its own repository acceptance gate. Passing a project gate
does not authorize public settings changes, registry writes, tag creation, or release
publication.

### 2.3 Ownership notation

Requirements marked **Repository** are implementable and verifiable without external
publication. Requirements marked **Owner gate** require a repository owner or an
authorized maintainer because they change public state, select a monitored contact,
use protected credentials, or create immutable public release references.

Repository-completable Phase 11 work may be prepared and reviewed locally. Because
the repository is already public, no Phase 11 branch, commit, or pull request may be
pushed and no Phase 11 change may be merged until Project A has all three written
prerequisites: favorable owner/legal approval for the Jhin name and coordinates, a
monitored confidential Code of Conduct enforcement contact, and an enabled/tested
private vulnerability-reporting route. Those are public-disclosure prerequisites,
not Project E release-publication actions.

## 3. Project A — Identity and community

### 3.1 Repository artifacts

This project creates or completes:

- `README.md`;
- `LICENSE`;
- `NOTICE`, only when the dependency attribution review finds notices that must be
  redistributed;
- `CONTRIBUTING.md`;
- `SECURITY.md`;
- `CODE_OF_CONDUCT.md`;
- `SUPPORT.md`;
- `CHANGELOG.md` following Keep a Changelog headings and SemVer versions;
- `.github/ISSUE_TEMPLATE/bug.yml`;
- `.github/ISSUE_TEMPLATE/feature.yml`;
- `.github/ISSUE_TEMPLATE/config.yml`;
- `.github/pull_request_template.md`;
- `.github/CODEOWNERS` for release, security, deployment, and connector-sensitive
  paths.

The README becomes the public entry point. It contains:

1. one-sentence positioning and an explicit current release status;
2. the actual public clone URL;
3. a Docker-only quick start using the release bundle;
4. one deterministic no-credential demo path;
5. embedded current screenshots;
6. supported host platforms and minimum resource guidance;
7. links to architecture, deployment, backup/restore, upgrades, demo,
   contributing, support, security, conduct, changelog, and license documents;
8. a concise warning that connector credentials are stored encrypted and the master
   key must be backed up separately;
9. the image namespace and verification command;
10. the distinction between self-hosted core operation and optional managed
    providers.

`CONTRIBUTING.md` defines the uv and pnpm setup, every `make` verification target,
the connector contribution path, migration discipline, test expectations, commit and
PR conventions, generated-file policy, security-sensitive review paths, and the
Apache-2.0 inbound contribution rule. Contributions use Developer Certificate of
Origin sign-off; no separate contributor license agreement is introduced for
`v0.1.0`.

`SECURITY.md` makes GitHub private vulnerability reporting the canonical confidential
channel. It explicitly names secret exposure, sandbox escape, authentication or
authorization bypass, cross-workspace access, webhook authenticity, supply-chain
compromise, and credential leakage as non-public reports. It lists `v0.1.x` as the
supported line after publication and defines acknowledgement within three business
days, initial assessment within seven business days, coordinated disclosure, and
credit by consent. It never asks reporters to include live credentials.

`CODE_OF_CONDUCT.md` adopts Contributor Covenant 2.1 and covers repository, issue,
review, community, and project-representation spaces. Its confidential enforcement
route is the monitored address selected at the owner gate; the file is not merged
with an unmonitored or fabricated address.

Issue forms collect reproducible environment/version information and redact secrets
by instruction. `config.yml` disables blank issues, links support questions to
`SUPPORT.md`, and links vulnerability reports to the private advisory form. The PR
template requires tests, documentation, migration and compatibility notes, security
impact, and confirmation that no credentials or generated local state are included.

### 3.2 Naming and licensing checks

**Repository:** a naming check scans tracked public content for superseded product
identifiers and permits only an explicit historical-reference allowlist. The
untracked user-owned production-plan reference is outside release artifacts and must
not be staged, edited, or packaged. A metadata check requires every distributed
Python project to declare `license = "Apache-2.0"`, the repository README, and the
canonical repository URLs. OCI images receive
`org.opencontainers.image.licenses=Apache-2.0`, source, revision, version, title,
description, and created labels.

**Owner gate:** the owner and authorized legal reviewer provide a favorable written
approval covering the Jhin name, relevant trademark conflict, package registries,
repository slug, and intended domains, with the decision date and scope recorded in
the release approval issue. Silence, an unresolved conflict, a conditional response
whose conditions are not satisfied, or an unfavorable result blocks every registry,
tag, release, public branch, pull-request, and merge write. The blocked release must
create a separate rename design and implementation plan, then repeat naming,
metadata, artifact-coordinate, screenshot, documentation, and legal review before
any Phase 11 work is made public.

### 3.3 Acceptance

- all required artifacts exist and contain no internal phase-progress prose;
- all Markdown links and anchors pass an automated link check;
- issue forms pass GitHub form-schema validation;
- license metadata and OCI-label tests pass;
- a tracked-content naming scan passes with a reviewed historical allowlist;
- an owner has configured and tested the private vulnerability route and confidential
  conduct contact before any Phase 11 public push, pull request, or merge;
- favorable written owner/legal approval for the Jhin name is attached to the release
  approval issue before any Phase 11 public push, pull request, or merge.

## 4. Project B — Operator onboarding and documentation

### 4.1 Documentation architecture

The documentation tree gains these public entry points:

```text
docs/
  architecture/
    README.md
    system-overview.md
    data-and-authority.md
    security-boundaries.md
    existing topical documents...
  deployment/
    compose.md
    configuration.md
    reverse-proxy-and-tls.md
    backup-and-restore.md
    upgrades-and-rollbacks.md
    sizing.md
    troubleshooting.md
  demo.md
```

`docs/architecture/README.md` is the map, not another implementation history. It
links the existing connector, event, delegation/team, sandbox, and
Vercel/Supabase documents. The system overview accounts for web, API,
workflow-worker, agent-worker, tool-worker, event-worker, sandbox-runner, sandbox
jobs, PostgreSQL, NATS JetStream, Temporal, and the optional Temporal UI. It distinguishes
PostgreSQL as product truth, Temporal as durable workflow authority, NATS as event
transport, the API as control-plane authority, and the tool gateway/sandbox runner
as execution boundaries.

The release adds `services/tool_worker` as a real Temporal worker on a dedicated tool
task queue. Agent workflows send validated tool activities to that queue; the
tool-worker owns deterministic gateway authorization, approval-time revalidation,
secret resolution, connector calls, result sanitization, and audit persistence. The
agent-worker owns model reasoning and never becomes a second connector executor.
CLI activities reach sandbox-runner only from tool-worker over the isolated runner
network. This boundary implements the authoritative plan's tool-worker role rather
than publishing an alias of agent-worker.

The security-boundary document includes the Docker-socket trust boundary: only
`jhin-sandbox-runner` mounts it, jobs never receive it, and release verification
fails if any other service or job can reach it. Sandbox-runner itself always runs as
a non-root numeric UID. In rootful-engine mode, release initialization records the
socket's nonzero group ID and Compose supplies it as a supplementary group. In
rootless-engine mode, initialization records the non-root socket path and owner
UID/GID and runs sandbox-runner with that matching non-root identity. Startup rejects
UID `0`, a socket path outside the configured exact path, an inaccessible socket, or
an ownership/mode mismatch. There is no root-user, privileged-container, or
world-writable-socket fallback. Architecture diagrams are stored as text-source
Mermaid diagrams so they remain reviewable and render in GitHub.

### 4.2 Deployment contract

`docs/deployment/compose.md` supports Linux Docker Engine and Docker Desktop with
Compose v2. It uses the release bundle, not a source build. The documented flow is:

```text
download release bundle, checksum/signature assets, and verifier-tools archive
run the external Docker preflight command from VERIFY.md
run bin/jhin init
review generated configuration
run bin/jhin up
run bin/jhin migrate
complete browser onboarding
run bin/jhin verify as a secondary post-start runtime check
```

The release notes and `VERIFY.md` render a copy-paste preflight sequence containing
the release's actual platform, tag, SHA-256 digests, workflow identity, and OIDC
issuer—no unresolved variable or moving tag. On a host with only Docker and Compose,
the sequence:

1. runs the Cosign bootstrap container pinned by digest in
   `deploy/verification-tools.lock.json` to verify the platform-specific
   `jhin-verifier-tools-0.1.0-linux-amd64.docker.tar` or
   `jhin-verifier-tools-0.1.0-linux-arm64.docker.tar` blob and its keyless bundle;
2. loads that authenticated archive with `docker load`;
3. invokes its external command as `docker run --rm` with the release directory
   mounted read-only and the verifier image selected by its authenticated image
   digest: `verify-release /release`;
4. lets `verify-release` use its checksum-authenticated Cosign and Skopeo binaries to
   verify the outer signature/checksums, safely extract and verify the inner manifest,
   require the expected release-workflow certificate identity and GitHub OIDC issuer,
   inspect every digest-pinned Compose image, and verify image signatures, SBOM
   attestations, and provenance subjects before application code executes.

The bootstrap Cosign verification uses the attached offline verification bundle and
exact certificate-identity constraints, so executing the verifier does not trust an
unauthenticated script from the release tarball. Failure stops the installation
before `bin/jhin init` can generate or modify local state. `bin/jhin verify` remains a
secondary post-start configuration, health, migration, heartbeat, port-exposure, and
runtime-digest check; it cannot substitute for the external authenticity preflight.

`bin/jhin init` uses the digest-pinned API image from the authenticated bundle to generate, with
mode `0600`, a 32-byte base64 master key, a random PostgreSQL password, and a random
sandbox-runner bearer token. It refuses to overwrite any existing key or environment
file. No production credential has a development default.

The release Compose contract publishes only the web entry point by default. Direct
API publication is opt-in and documented for local administration only. PostgreSQL,
NATS, Temporal, Temporal UI, and sandbox-runner have no public bindings. The guide
contains tested Caddy configuration and protocol/headers guidance for Nginx- or
Traefik-compatible proxies. TLS is mandatory outside loopback use.

Configuration documentation classifies every variable as required, generated,
optional, secret, or development-only. It includes external Temporal configuration,
connector endpoint allowlists, volume locations, log level, application URL, and
master-key handling. Development fake credentials never appear as production
defaults.

Every supported backup covers the Jhin PostgreSQL data, complete NATS JetStream
state, complete Temporal persistence/history, optional object storage, and the master
key. The application data, JetStream, and Temporal captures use a documented
coordinated quiesce or snapshot sequence so their recovery point is explicit. The
master key is backed up separately from application data. A restore drill starts a
durable workflow, leaves it waiting on a real approval or timer, takes the coordinated
backup, restores onto empty volumes, and proves that the original Temporal history
and wait resume to completion. It also proves JetStream consumers resume from the
restored state without losing accepted events or duplicating durable work, credentials
can be decrypted, and audit records survive. The upgrade guide requires database backup, migration preview,
forward migration, application rollout ordering, health verification, and rollback
boundaries. Database downgrade is never promised when a migration declares itself
forward-only.

Sizing publishes all five measured Phase 10 profiles without collapsing monitored
and unmonitored production footprints:

| Profile | Host baseline | Intended load |
|---|---|---|
| Development | 4 vCPU, 8 GiB RAM, 40 GiB SSD | one developer, one active sandbox, monitoring disabled |
| Small production | 8 vCPU, 16 GiB RAM, 100 GiB SSD | up to 10 active agents, two concurrent runs, one concurrent sandbox |
| Small + bundled monitoring | 12 vCPU, 24 GiB RAM, 150 GiB SSD | small production plus 15-day metrics and 72-hour traces |
| Medium production | 16 vCPU, 32 GiB RAM, 250 GiB SSD | up to 50 active agents, ten concurrent runs, four concurrent sandboxes |
| Medium + bundled monitoring | 24 vCPU, 48 GiB RAM, 400 GiB SSD | medium production plus local monitoring retention |

Each profile publishes CPU, memory, disk, expected agent/tool/sandbox concurrency,
monitoring retention where enabled, measurement method, and the Phase 10 30-minute
load evidence. Values come from measured Phase 10 gates rather than estimates.
Troubleshooting covers health, logs, disk pressure, migration failures, NATS
redelivery, Temporal connectivity, master-key mismatch, and sandbox availability
without printing secrets.

### 4.3 Acceptance

- every first-party service and trust boundary has one authoritative documentation
  location;
- documentation uses released image coordinates and contains no source-build-only
  production path;
- a fresh Ubuntu host completes the literal guide with Docker and Compose as the only
  preinstalled product dependencies;
- the external Docker verifier authenticates its Cosign/Skopeo tool archive, bundle,
  checksums, image digests, signatures, SBOMs, and provenance before `bin/jhin init`;
  a tampered input fails without creating local Jhin state, and post-start
  `bin/jhin verify` remains a separate secondary check;
- Caddy TLS, backup/restore, master-key recovery, upgrade/rollback, and all five
  sizing profiles have dated evidence attached to the release approval issue;
- the restore drill proves complete JetStream state and original Temporal histories
  are restored and that approval/timer waits resume without duplicate durable work;
- rootful socket-GID and rootless socket-owner sandbox-runner modes pass as non-root,
  while UID `0`, inaccessible/mismatched sockets, privileged mode, and
  world-writable sockets fail closed;
- production configuration contains no development credential, fake service,
  development allowlist, or publicly exposed data/control port.

## 5. Project C — Deterministic demo, screenshots, and starter templates

### 5.1 Versioned starter-template catalog

Starter templates move from client-only constants and ad hoc seed values to one
versioned catalog resource:

`packages/domain/src/jhin_domain/resources/starter_templates.v1.json`.

The catalog has `schema_version: 1`, immutable stable IDs, display metadata, role
instructions, organization membership, manager relationships, workflow-template
selection, and least-privilege capability presets. It contains:

- agent templates: `cto`, `software-engineer`, `qa-engineer`, `devops-engineer`,
  `marketing-director`, `content-writer`, `seo-specialist`, and
  `generic-assistant`;
- organization templates: `engineering-team` and `marketing-team`;
- workflow template metadata for `engineering-ticket`, which refers to the existing
  durable implementation rather than duplicating workflow logic.

The API validates and serves the catalog. The web creation wizard consumes that API;
it no longer owns a divergent hard-coded template list. `jhin-seed-dev` instantiates
the versioned engineering and marketing organizations, records catalog version and
template ID in seed audit metadata, and preserves the current idempotent refusal to
overwrite an existing environment. Catalog upgrades add a new schema/catalog version
and never mutate user-created agents.

### 5.2 Demo mode

Demo mode remains a Compose development overlay and is never present in the release
production manifest. It includes the existing fake OpenAI-compatible provider,
GitHub, Linear, Vercel, Supabase Management API, and isolated Supabase fixture
database. All fake ports bind to loopback. Their credentials are conspicuously
development-only and accepted only by fake services.

The repository adds one orchestration command:

```text
make demo
```

It performs, idempotently, the following sequence:

1. creates the local master key if absent;
2. builds the sandbox image and starts the development stack;
3. applies migrations and runs the versioned seed;
4. configures fake Linear's webhook from the seeded connection through a
   development-only management command;
5. transitions seeded issue `ENG-142` from Backlog to Todo;
6. polls the resulting trigger invocation, task, durable agent run, delegated QA
   result, and Linear outcome comment;
7. prints the local task URL and a compact pass/fail summary.

The management command refuses to run unless `APP_ENV=development`, the target
connection is explicitly marked as seeded development data, and its normalized
origin is the in-stack fake Linear service. It exposes no HTTP endpoint and cannot
configure or invoke a production connector. Repeated execution either reports the
existing successful demo or resets only fake-service and seed-owned demo state using
exact IDs; it never broadly deletes user data.

### 5.3 Screenshot and demo assets

Playwright captures reviewed assets from demo mode at deterministic desktop
`1440x1000` and mobile `390x844` viewports with local fonts, fixed locale/timezone,
reduced motion, and stable seeded content. The public set is stored under
`docs/assets/screenshots/` and includes:

- organization/team view;
- agent template and configuration flow;
- trigger builder;
- running task timeline with delegation/QA;
- approval/tool-access surface;
- connector access summary.

`docs/demo.md` gives the one-command path, expected duration, expected output, reset
semantics, and an explicit statement that no external account or real credential is
used. README embeds a focused subset. Screenshot generation is reproducible, but
image changes remain human-reviewed rather than blindly overwritten in CI.

### 5.4 Acceptance

- catalog schema validation, stable-ID uniqueness, API serialization, UI rendering,
  seed idempotency, and non-overwrite behavior have automated tests;
- `make demo` passes twice from a clean checkout and once after worker restart;
- a production-Compose negative assertion proves no fake service, fixture database,
  fake credential, demo management command, or development connector allowlist is
  reachable in production;
- all published screenshots are generated from fake data, contain no credential or
  local absolute path, match the current UI, and pass human visual review;
- the README demo contains no value the user must discover manually.

## 6. Project D — Supply chain and release automation

### 6.1 Workflow topology

The repository provides:

- `VERSION` — the authoritative plain SemVer value shared by release validation;
- `.github/workflows/ci.yml` — Python and web lint/type/unit tests, production builds,
  all eight first-party container builds, and Compose integration tests;
- `.github/workflows/e2e.yml` — clean full-stack browser and accessibility tests;
- `.github/workflows/security.yml` — CodeQL for Python and JavaScript/TypeScript,
  secret scanning, dependency audit, filesystem/configuration scan, and scheduled
  image/base-image review;
- `.github/workflows/release.yml` — release-candidate dry run and protected tag
  publication;
- `renovate.json` — weekly grouped updates for uv, pnpm, GitHub Actions, Docker base
  images, and release lock data.

Every third-party action in security and release workflows is pinned to a full commit
SHA with its human-readable version in a comment. Workflow permissions default to
read-only and are elevated per job. Pull-request workflows do not receive release
credentials. Publication jobs use a protected `release` environment and exactly
these GitHub permissions where needed: `contents: write`, `packages: write`,
`id-token: write`, and `attestations: write`. CodeQL uses `security-events: write` in
its separate workflow. No long-lived registry token is stored when `GITHUB_TOKEN`
can publish to GHCR.

`release.yml` supports a manual non-publishing candidate run on a commit from
canonical `main`. A tag run accepts only `vMAJOR.MINOR.PATCH` or
`vMAJOR.MINOR.PATCH-rc.NUMBER`. Its first job has read-only permissions and verifies,
before any registry, attestation, alias, or GitHub Release write, that the reference
is an annotated tag object, `git verify-tag` succeeds, and the SSH signing principal
and public key match a reviewed entry in `.github/release-allowed-signers`. It then
verifies the tagged commit is on `main` and that tag version equals root `VERSION`,
every Python project version, and the web package version. Publication jobs cannot
start unless this job succeeds. The owner adds or rotates real release signing keys
through an ordinary code-reviewed change before tagging. `v0.1.0` is the first stable
tag. A failed published version is never retagged or force-moved; the fix receives the
next SemVer version.

### 6.2 Image matrix

All application images support `linux/amd64` and `linux/arm64`, use multi-stage
builds, and run as non-root. Sandbox-runner reaches the configured Docker or rootless
Docker socket only through the exact non-root UID/GID or supplementary socket GID
recorded by release initialization. Its entry point fails closed if access would
require root, privileged mode, a world-writable socket, or an unconfigured socket
path.

| OCI repository | Dockerfile/build selection | Runtime purpose | Required runtime user |
|---|---|---|---|
| `ghcr.io/teachmetech/jhin-web` | `apps/web/Dockerfile` | Next.js UI | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-api` | `docker/python.Dockerfile`, `SERVICE_PACKAGE=jhin-api` | FastAPI control plane and release init tooling | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-workflow-worker` | shared Python Dockerfile, `jhin-workflow-worker` | general Temporal workflows | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-agent-worker` | shared Python Dockerfile, `jhin-agent-worker` | model reasoning and agent execution | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-tool-worker` | shared Python Dockerfile, `jhin-tool-worker` | deterministic tool gateway and connector execution | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-event-worker` | shared Python Dockerfile, `jhin-event-worker` | JetStream normalization and trigger matching | non-root `jhin` |
| `ghcr.io/teachmetech/jhin-sandbox-runner` | shared Python Dockerfile, `jhin-sandbox-runner` | isolated job-container lifecycle | configured non-root socket identity |
| `ghcr.io/teachmetech/jhin-sandbox` | `docker/sandbox.Dockerfile` | default ephemeral CLI job environment | UID/GID `1000:1000` |

`jhin-tool-worker` must be independently runnable, have its own package, entry point,
healthcheck, task queue, Compose service, tests, SBOM, provenance, and image digest.
An image that merely changes the command of `jhin-agent-worker` without enforcing the
reasoning/execution boundary does not satisfy the matrix.

The release workflow publishes a multi-platform OCI index for each repository. Every
stable release receives immutable tags `MAJOR.MINOR.PATCH` and `sha-` followed by the
first 12 lowercase hexadecimal characters of the release commit.
Stable releases additionally update `MAJOR.MINOR`, `MAJOR`, and `latest` only after
all publication verification passes. A release candidate receives only its exact
`MAJOR.MINOR.PATCH-rc.NUMBER` and commit tag; it never updates moving tags. Compose
and release notes resolve exact digests rather than moving tags.

### 6.3 Release Compose and bundle

`deploy/compose.release.template.yaml` is a production-only, pull-based manifest.
It contains no `build` keys and no fake/demo services. `deploy/third-party-images.lock.json`
pins PostgreSQL, NATS, Temporal, Temporal UI, and other bundled third-party images by
tag and digest. Renovate updates that lock through ordinary reviewed PRs.

The non-publishing candidate run exports every first-party image as a separate OCI
image-layout tarball for each platform. Names have the exact form
`jhin-COMPONENT-0.1.0-linux-amd64.oci.tar` and
`jhin-COMPONENT-0.1.0-linux-arm64.oci.tar`; `COMPONENT` is one of the eight image
repository suffixes in the matrix. `candidate-image-lock.json` records the release
commit, platform, OCI manifest digest, configuration digest, and archive checksum for
all sixteen artifacts. Candidate SBOM, provenance, signature, and scan subjects bind
to those recorded manifest digests.

The candidate and final release artifacts also include a platform-specific
verifier-tools image archive containing pinned Cosign and Skopeo executables plus a
Docker-loadable, digest-pinned OCI Distribution registry image. The verifier build
checks both executable archives against their upstream signed SHA-256 release
manifests before installing them; the resulting image archive is then covered by the
candidate/release checksums and a keyless Cosign blob signature. These are
verification dependencies, not Jhin release images, and their versions, upstream
checksums, archive checksums, signatures, and licenses are recorded in the evidence
manifest and `deploy/verification-tools.lock.json`.

Pre-publication clean-room jobs download only the candidate workflow artifacts, load
the verifier registry image without a network pull, start it on an isolated Docker
network with its host port bound to loopback, and use the bundled Skopeo executable
to copy the native-platform OCI archives into that registry. The candidate renderer
creates `jhin-0.1.0-candidate-linux-amd64-compose.tar.gz` and
`jhin-0.1.0-candidate-linux-arm64-compose.tar.gz`; each contains a Compose file whose
eight Jhin image references use the loopback registry plus the recorded native
manifest digests. The registry has no mirror, push-through cache, or route to GHCR,
and the job uses an empty Docker credential directory. This proves the exact
candidate on both native platforms before any Jhin package is published.

After the tag workflow has created and verified the multi-platform image indexes, the
final release renderer creates
`jhin-0.1.0-compose.tar.gz`. Its root contains:

```text
compose.yaml                 digest-pinned first- and third-party images
.env.release.example        schema/comments only; no usable secret defaults
bin/jhin                     Docker-only init/up/migrate/verify/backup helpers
image-lock.json              platform indexes and exact image digests
MANIFEST.SHA256              non-recursive checksums for extracted bundle content
VERIFY.md                    rendered external Docker-only preflight commands
LICENSE
README.md
SECURITY.md
docs/deployment/
```

The renderer fails if any image remains tag-only, any service has a build key, any
required service lacks a healthcheck, or any fake/development token appears.
`MANIFEST.SHA256` is generated before archiving from a byte-sorted list of every
regular bundle file except `MANIFEST.SHA256` itself; paths are relative POSIX paths,
and symlinks are forbidden. It therefore verifies extracted content without
recursion. After the Compose tarball, platform verifier-tools archives, SBOM JSON
files, provenance JSON files, and image-lock file are finalized, the outer
release-level `SHA256SUMS` is generated from an explicit byte-sorted allowlist of
exactly those primary assets. It excludes
`SHA256SUMS` itself, every `.sig` or `.bundle` signature artifact, GitHub-generated
source archives, and the workflow evidence manifest. Signature artifacts are created
only after `SHA256SUMS`; Cosign signs each primary asset and the finalized checksum
file, and those signatures are verified cryptographically rather than recursively
checksummed. `MANIFEST.SHA256` never names itself or its containing tarball, and
`SHA256SUMS` never names itself or any signature derived from it.

### 6.4 SBOM, provenance, signing, and scanning

BuildKit emits `mode=max` SLSA provenance and native SBOM attestations for every
multi-platform image. Syft also generates one downloadable SPDX 2.3 JSON document
per image index and `jhin-0.1.0-source.spdx.json` for the source tree. Asset names
use the OCI repository suffix, version, and `.spdx.json` extension.

Cosign keyless signing uses GitHub Actions OIDC. Every image index digest is signed;
the Syft SPDX document is attached as an in-toto attestation; the Compose tarball and
release-level `SHA256SUMS` receive keyless blob signatures with verification bundles.
Verification instructions constrain certificate identity to the repository release workflow and
OIDC issuer to GitHub Actions, not merely to any valid Fulcio identity.

Security gates are exact:

- Gitleaks finds no verified secret in tracked content or release artifacts;
- CodeQL, dependency, filesystem, configuration, and image reports have zero open
  critical findings, regardless of when a finding was introduced, whether a fix is
  available, or which component owns it; critical findings have no release exception;
- Trivy scans source, configuration, each platform image, and each final image index;
- an open high finding may ship only through a narrow, owned exception in
  `.security-exceptions.yml` containing the scanner and finding ID, exact affected
  component/image and version, exposure analysis, compensating control, accountable
  owner, tracking issue, approver, and an expiry no more than 30 days away;
- exception tooling may derive scanner-specific suppression input such as
  `.trivyignore.yaml`, but the derived file cannot broaden scope or suppress critical,
  medium, low, unknown, or unrelated high findings;
- expired, unowned, structurally incomplete, unmatched, or overbroad high exceptions
  fail CI;
- runtime dependencies with AGPL, SSPL, BUSL, or unknown licensing fail the license
  policy unless the owner records a distribution review in the release approval
  issue;
- release SBOMs, attestations, signatures, checksums, and scan summaries are retained
  as release assets and linked from release notes.

The protected tag job reruns all gates. The manual candidate run produces the sixteen
per-platform OCI-layout artifacts and all derived evidence without registry writes,
tags, or GitHub Release creation.

### 6.5 Acceptance

- CI, E2E, security, and candidate-release workflows pass at the release commit;
- all eight images build and execute smoke tests on native `linux/amd64` and native
  `linux/arm64` runners;
- all sixteen OCI-layout candidate artifacts match `candidate-image-lock.json` and
  pass full-stack clean-room verification through the isolated loopback registry;
- both verifier-tools archives contain the locked Cosign and Skopeo versions, match
  upstream signed checksums and release checksums, and pass keyless blob verification
  before execution;
- the generated bundle is digest-only, contains no development surface, and verifies
  offline metadata before startup;
- an independent verification job validates OCI signatures, provenance subject
  digests, SPDX attestations, blob signatures, and certificate identity constraints;
- no publication credential is available to pull-request code.

## 7. Project E — Publication gate

### 7.1 Repository-completable preflight

The release commit must be on canonical `main`, the worktree used for artifacts must
contain only tracked release content, and the pre-existing untracked production-plan
reference must be absent from the archive. The candidate workflow creates an evidence
manifest containing commit, version, test run URLs, image build digests, scan results,
documentation checks, demo results, and clean-room results.

Pre-publication clean-room verification runs on native Ubuntu `linux/amd64` and
native Ubuntu `linux/arm64` hosts with no repository checkout, package cache, prior
volumes, Jhin credentials, or GHCR access. It downloads the candidate bundle and
native OCI-layout artifacts, verifies signatures and non-recursive checksums, loads
the eight Jhin images into the isolated loopback registry, initializes random
secrets, starts the digest-pinned native candidate stack, applies migrations,
completes onboarding through supported interfaces, exercises health and worker
recovery, restarts the stack, and verifies preserved state. A separate source-checkout
job runs `make demo` with fake services.

After the tag workflow pushes verified image indexes and the owner makes all eight
GHCR packages public, a second pair of fresh native clean-room runners uses an empty
`DOCKER_CONFIG`, performs no `docker login`, and anonymously pulls every image by the
digest in the final release bundle. It repeats installation, migration, health,
restart, and state-preservation checks. No GitHub Release object is created and no
moving image alias is updated until both anonymous-pull jobs pass.

The publication preflight also consumes current evidence for every production-ready
criterion in section 49: deterministic migrations; tested backup/restore and master
key recovery; Temporal recovery; NATS redelivery/idempotency; workspace isolation;
webhook signatures; secret redaction; sandbox escape-risk review; rate limits;
approval gates; health, logs, and critical metrics; dependency/container scanning;
fresh-machine onboarding; and a real GitHub-plus-Linear end-to-end run. For the first
tag there is no prior tagged version to upgrade from; beginning with `v0.1.1`, an
upgrade from the immediately previous supported tag is mandatory.

### 7.2 Later Project E publication actions

The favorable Project A owner/legal decision, monitored conduct contact, and tested
private vulnerability route are prerequisites to any public Phase 11 push or merge;
they are not deferred to this list. After those prerequisites are evidenced and
Projects A–D pass locally, an authorized owner performs or approves these later
release-publication actions in order:

1. configure the remaining public repository release settings: canonical `main`,
   protected branch, required CI/security reviews, issue forms, and protected
   `release` environment;
2. confirm GHCR package ownership and allow the workflow's scoped package writes;
3. supply environment-protected credentials for the single real GitHub-plus-Linear
   acceptance run; the workflow stores only a redacted outcome summary;
4. approve the successful non-publishing release-candidate run;
5. create the signed annotated `v0.1.0` tag on the approved commit using an identity
   already present in `.github/release-allowed-signers`, run the same
   allowlist-backed `git verify-tag` check locally, record its successful output in
   the approval issue, and only then push the tag;
6. after the read-only tag job verifies annotation, signature, signer allowlist,
   main ancestry, and version consistency, approve the protected image-publication
   environment;
7. after private-package digest/signature verification succeeds, make all eight GHCR
   packages public without creating a GitHub Release;
8. require the fresh anonymous-pull amd64 and arm64 clean-room jobs to pass with an
    empty Docker credential directory;
9. update stable moving image aliases, verify them against the release digest lock,
    and only then create and publish the GitHub Release.

No implementation task may infer authority for these actions. Real connector/model
credentials are never required for demo or screenshot generation and must not be
copied into repository files or ordinary CI logs.

### 7.3 Failure and rollback rules

- Before the tag is pushed, a failed gate changes no public release state; fix the
  commit and rerun the candidate workflow.
- After a tag is pushed, the tag is never moved or reused. A defect is fixed in the
  next patch version.
- Registry packages remain non-public and no GitHub Release object exists until
  post-push digest/signature verification succeeds. Packages become public only for
  the anonymous-pull gate; failure at that gate leaves them public by immutable
  digest, creates no release, and requires a new patch version rather than mutation.
- If a published image is unsafe, moving aliases are redirected only to a newly
  verified patch release; the affected immutable tag/digest remains auditable and is
  marked withdrawn in release notes and the security advisory.
- Database and master-key recovery follow the documented restore procedure. Release
  automation never deletes operator volumes or replaces a master key.

### 7.4 Final release evidence

The public release is complete only when:

- favorable written owner/legal approval for the Jhin name is linked from the
  release evidence;
- `v0.1.0` is a signed annotated tag on the reviewed canonical-main commit and
  the recorded local pre-push check plus the release workflow's `git verify-tag`
  prove its principal/key is in `.github/release-allowed-signers` before the tag push
  and before any release-workflow write, respectively;
- the GitHub Release is public and contains changelog, Compose bundle, checksums,
  signatures, source and image SBOMs, provenance/signature instructions, image
  digests, compatibility statement, and known limitations;
- all eight public GHCR image indexes match the release digest lock and verify with
  Cosign;
- isolated-registry pre-publication and anonymous-GHCR amd64/arm64 clean-room
  installation evidence is linked;
- the README quick start and screenshots match the released artifacts;
- the Phase 11 checklist is updated only after each corresponding evidence link is
  recorded.

## 8. Phase 11 checklist closure matrix

| Phase 11 item | Design owner | Required acceptance evidence |
|---|---|---|
| Choose final name | A + owner gate | authoritative Jhin decision, metadata scan, favorable written owner/legal approval; unresolved conflict blocks release and starts rename planning |
| Apache-2.0 license | A | complete license, distribution metadata, OCI labels, attribution review |
| README | A/B/C/D | public clone and external Docker-only authenticity preflight precede init; digest-bundle quick start then passes on clean host; all links and screenshots valid |
| Architecture docs | B | system/authority/trust-boundary map accounts for every released service and links topical docs |
| Deployment guide | B/E | clean-room install, TLS, coordinated PostgreSQL/JetStream/Temporal backup and restored-wait resumption, master-key recovery, upgrade/rollback, sizing evidence |
| Contributor guide | A | fresh contributor setup/change verification and connector example path pass |
| Security policy | A + owner gate | supported versions, private disclosure route, response policy, tested repository setting |
| Code of conduct | A + owner gate | Contributor Covenant 2.1 plus tested monitored confidential enforcement contact |
| Screenshots/demo | C | reviewed fake-data assets and twice-repeatable `make demo` without manual identifiers or secrets |
| Seeded starter templates | C | versioned catalog, API/UI parity, idempotent seed, non-overwrite upgrade tests |
| Fake/demo connector mode | C | complete fake flagship workflow and production negative-isolation assertion |
| Issue templates | A | validated bug/feature forms, chooser/security/support routing, PR template |
| Release automation | D | OCI-layout candidate workflow, annotated-tag/signature/signer validation before writes, version validation, reproducible artifact evidence |
| Container images | D/E | eight signed multi-arch public indexes including a real tool-worker, digest lock, isolated-registry and anonymous-pull Compose smoke tests |
| SBOM/security scanning | D | source/image SPDX, provenance, signatures, zero critical findings, only narrow owned expiring high exceptions, authenticated Cosign/Skopeo verifier, scan enforcement |
| First tagged release | E + owner gate | allowlisted-signer-verified annotated `v0.1.0`, eight anonymous-pullable images, release assets, two-stage clean-room evidence |

## 9. Cross-project dependency order

Project A can merge first, but because the repository is already public it cannot be
pushed or merged until its owner/legal/contact prerequisites pass. Project B depends
only on stable artifact names from this design. Project C depends on the existing
fake services and template behavior, not on publication. Project D depends on the
fixed image matrix and documentation bundle contract. Project E starts only after
A–D acceptance gates pass.

The decomposition therefore supports separate specifications and implementation
plans for A, B, C, and D, followed by a short publication-runbook specification for
E. No sub-project is allowed to mark the Phase 11 checklist complete on its own.

## 10. Design consistency constraints

- Jhin is the authoritative product name while `APP_NAME` remains configurable;
  publication additionally requires favorable written owner/legal approval, and an
  unresolved conflict blocks release and starts a separate rename plan.
- `v0.1.0` is the first public tag and matches every distributed component version.
- The first release publishes OCI images and a Compose bundle, not npm or PyPI
  packages.
- The production release is pull-based and digest-pinned; source builds remain a
  contributor/development path.
- Demo and screenshots use only fake services; the one real GitHub-plus-Linear run
  is a protected production-readiness gate owned externally.
- Repository work creates no tag, release, registry package, public setting, contact,
  or credential.
- All eight first-party images support amd64 and arm64, including an independently
  runnable tool-worker.
- Sandbox-runner and every sandbox job run non-root; socket access uses only the
  configured non-root identity or supplementary socket GID and has no root fallback.
- Pre-publication clean-room verification uses per-platform OCI-layout artifacts and
  an isolated loopback registry; post-publication verification uses anonymous GHCR
  pulls before any GitHub Release is created.
- A release tag is immutable. Failures after tagging advance the version rather than
  rewriting history.
- Phase 11 completion does not bypass any Phase 10 or section 49 production-ready
  requirement.
