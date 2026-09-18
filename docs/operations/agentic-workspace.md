# Agentic chat workspaces

Jhin stores each new chat's working files in a persistent Docker volume. Files
uploaded or published in chat also receive immutable versions in the
`managed_files` volume, with their metadata in PostgreSQL. Restarting a worker,
terminal, or preview does not remove those versions or the chat's working disk.

## Installation and upgrade

1. Back up PostgreSQL, managed files, and retained sandbox workspace volumes.
2. Build or pull the matching API, agent, tool, workflow, web, and runner images.
   The runtime gateway uses the tool-worker image with `jhin-runtime-gateway`.
   Build the sandbox image from `docker/sandbox.Dockerfile` for Node.js and the
   included Python/Office document tools.
3. Apply the sequential additive migrations through **0047** before starting the
   new application workers: 0045 adds files/projects/workspaces, 0046 adds the
   conversation journal and generation attempts, and 0047 adds runtime sessions.
4. Start the new services, including `runtime-gateway`. Check their health checks,
   upload a file, run a command, refresh the chat, and reopen the saved file.

The repository's normal Alembic invocation remains authoritative. Test upgrade
and rollback against a restored database before a production upgrade. A rollback
of application code can leave the additive tables and volumes in place; dropping
the new tables would remove revision and conversation metadata. Never delete a
volume as part of an application rollback.

`JHIN_AGENTIC_WORKSPACE=0` disables the new runtime routes. The additive history
and stored files remain intact. This is a rollout switch, not a cleanup command.
Set `NEXT_PUBLIC_AGENTIC_WORKSPACE=0` and rebuild the web image to restore the
legacy chat interface during rollout. Browser flags are compiled into that image.

Existing agent-owned workspaces remain where they were. In an older chat, use
**Copy previous workspace** to create its own working copy. Copy refuses files
that changed or cannot be safely captured and reports exclusions. It does not
move or delete the source. New chats can select a saved project; **Save this
workspace as project source** captures an immutable starting revision for later
chats. A saved project retains its own blob references, so removing the source
chat does not remove the saved source. Repository URL/ref and project context
remain reusable configuration.

## Proxy and origin configuration

The standard web service proxies `/api/*` to the API and `/runtime/*` to the
runtime gateway. Keep both routes on the same user-facing origin. Support HTTP
upgrade for `/runtime/*`, disable SSE response buffering for conversation event
streams, and allow an upload body of at least 26 MiB if the advertised 25 MiB file
limit is desired. The API's default overall ceiling is 32 MiB. A proxy with a
lower limit must give users a clear 413 response.

The gateway has no published host port in Compose. Only the runner has Docker
authority. The API and agent worker do not acquire the runner token or join the
runner network. Browser connection tickets scope access to one workspace,
session, user, expiry, and ownership generation. Terminal tickets travel in a
WebSocket subprotocol. Preview capability paths must not be recorded in proxy
access logs or analytics.

The preview transport supports localhost, local LAN HTTP, and HTTPS installations.
The API's existing transport policy still applies: `APP_ENV=production` or
`staging` permits plaintext HTTP only on loopback hosts. For a local LAN HTTP
installation, set `APP_ENV=dev`, `COOKIE_SECURE=false`, and `APP_URL` to the actual
browser origin, for example `http://192.168.1.20:3000`, then recreate the services.
For production HTTPS, use `APP_ENV=production`, `COOKIE_SECURE=true`, and the
actual `https://` origin. A public domain is not required for the local profile.

Previews run from a published source revision in a separate container. Jhin enforces
an opaque browser origin with a sandboxed iframe and response-level CSP, including
direct navigation and error responses. Dashboard cookies and authorization
headers are not forwarded to preview programs. HTTP and WebSocket requests only
reach the registered loopback port of that preview process. Applications needing
provider login, service workers, or persistent cookies are outside this profile.
For framework compatibility, preview scripts see an empty `document.cookie`;
writes are ignored. This does not grant access to dashboard cookies, change the
opaque origin, or enable provider login inside a preview.

**Refresh from changes** publishes a new source revision and starts a replacement
preview; **Restart** starts the same published source again. Neither mounts the
mutable authoring directory into the preview.

## Ownership and retained state

Owners and admins can **Take control** of the editor and terminal. Jhin confirms
the workspace fence before accepting input. If an agent is still active, stop
its turn and wait for confirmed completion. **Return to agent** stops the human
terminal before releasing ownership. Read-only viewers can inspect retained
output but cannot send input or resize a controlled terminal.

PTY output replay is bounded to 131,072 characters. Clients use the server's
offset and never replay uncertain keystrokes. Input namespaces change with new
connection tickets, so reconnecting cannot silently drop new commands or repeat
old ones. Terminal and preview sessions expire and are cleaned up; their
published files remain available. A runner restart ends its processes and is
reported as such, rather than silently starting commands again.

Before an agent begins file operations, Jhin captures a supported-file checkpoint.
After its run, it snapshots supported outputs. Colleagues work in isolated copies
and publish under `colleagues/<run-id>/`; their results do not overwrite the primary
chat's tree. Review and apply selected work explicitly. File restores check current
versions and ownership; they cannot undo external app actions.

Snapshots are bounded to 256 files and 32 MiB in total, with a 25 MiB individual
file limit. `.git`, dependencies, build caches, symlinks, environment files, and
other unsupported or oversized files are listed as exclusions. Terminal working
disks can contain more than a checkpoint supports. Quota refusal preserves these
disks. Archive retains files; no age-based cleanup removes conversation or
colleague volumes.

## Storage limits

`JHIN_FILES_WORKSPACE_QUOTA_BYTES` limits managed uploads and immutable published
versions per organization workspace. Its default is **10 GiB (10,737,418,240
bytes)**; set a positive byte count in the installation environment and recreate
the application services to apply a change. Both local and release Compose
configurations pass this setting to the file-consuming services.

The limit counts physical bytes in that workspace's managed-file storage. Files
with identical SHA-256 hashes share one blob and count once. Archived chats,
saved project sources, earlier versions, and blobs left by interrupted
publications still count. A lock in the shared local volume serializes writers
across API and tool-worker processes, so simultaneous uploads cannot both reserve
the same remaining capacity. Use storage that supports local filesystem locking
and hard links.

At the limit, publication of new bytes returns **413**. Existing downloads and
publication of an already-stored identical blob continue to work. Jhin does not
delete retained files to make room. Raising the configured limit permits further
uploads; exporting or archiving alone does not reduce stored bytes. There is
also a 100,000-file storage traversal ceiling. Do not manually remove hash blobs
without a complete metadata-and-volume recovery plan: several chats or projects
may refer to the same bytes.

Managed-file storage is separate from the sandbox working-disk quota. File
operations and terminal closure report a fresh disk measurement to the control
plane. Partial measurements remain marked unknown, and stale measurements cannot
replace newer accounting. Preview runtime state is disposable and does not
replace the authoring disk's measurement.

Human editor saves, terminal creation, and new terminal input use the same
`SANDBOX_WORKSPACE_MAX_MB` and `SANDBOX_WORKSPACE_TOTAL_MAX_MB` admission limits as
agent work. Recorded excess usage or an unknown partial measurement refuses
further growth with **507**. Reads/export, zero-length truncation or deletion,
interrupt, resize, stop, and returning control remain available. These checks use
the latest measurement; they are not a hard disk cap while a command runs. No
quota check deletes files automatically.

## Backup and recovery

For a consistent backup, wait for active commands and writes to finish, then
quiesce API, agent/tool workers, runtime gateway, and runner. Back up these three
sets together:

- PostgreSQL, including conversation journal, project, file revision, checkpoint,
  runtime session, and sandbox workspace metadata.
- The entire `managed_files` volume. A metadata-only backup cannot recover uploads
  or published deliverables. Restore with UID/GID 10001 ownership for API/tool-worker
  writes; gateway and agent-worker mounts are read-only.
- All retained sandbox volumes named by `sandbox_workspace.workspace_key`, using
  the runner's volume-name prefix. These include conversation, colleague, and
  legacy agent disks; preserve their UID/GID 1000 ownership.

Restore matching PostgreSQL metadata and volumes while writers are stopped, apply
the matching migrations, and start services. Validate a downloaded revision's
SHA-256 against its recorded digest. Runtime tickets and processes are disposable;
reopen sessions after recovery instead of replaying terminal input. If a blob is
missing, restore the managed-files volume; Jhin reports storage unavailability
rather than fabricating a replacement.

`apps/api/tests/test_file_backup_restore.py` repeats metadata-and-volume restoration
into independent paths and verifies that an archived chat can read both its
original pinned input and its newer file revision. This complements PostgreSQL
migration tests; it does not replace an operator's restoration drill for the full
installation.

## Repeatable verification

`scripts/verify_agentic_runner.py` tests actual Docker file guards, chat isolation,
PTY interaction, internet, document generation, and retained files.
`scripts/verify_agentic_previews.py` tests static, HTTP POST/WebSocket, Vite, and
Next.js adapters. Both use disposable names and avoid reaping existing runner
jobs. They require the runner's normal configured Docker authority.

`scripts/verify_agentic_chat.py` exercises authenticated API workflows through
`--phase journal`, `review`, `project`, `baseline`, and `recover`, with explicit
`--workspace` and `--conversation` IDs. Supply the credential through the process
environment variable `JHIN_LIVE_API_KEY`; the script does not print it. Journal
and recovery checks are read-only. Review and project checks create revisions,
checkpoints, branches, and project copies, so run them against the sample sales
workflow. Capture a baseline before a controlled restart, then run recovery with
the same `--evidence` path to compare authoritative items, metadata, and file bytes.

Run PostgreSQL journal tests against a separate `TEST_DATABASE_URL`, and browser
acceptance against a test workspace. The implementation ledger records commands,
outcomes, and remaining release gates in `docs/testing/agentic-workspace-progress.md`.
The recorded local upgrade to 0047 and completed sample document workflow do not
constitute full release acceptance. Live browser terminal/preview checks require
an owner or admin to sign in normally; the isolated acceptance installation uses
its own normally created owner account. Fixture-based browser checks are recorded
separately from actual authenticated transport checks.

The existing OpenTelemetry pipeline exports `conversation_event_delivery_seconds`
(journal age when delivered, including replay), `conversation_recovery_seconds`
(time for a resumed SSE request to drain its backlog), and
`conversation_reconnects_total` (opened, caught-up, or snapshot-required streams).
`artifact_publications_total` records successful and failed inspection/blob writes;
`runtime_stuck_sessions` counts overdue sessions and lifecycle transitions older
than two minutes. `runtime_session_cleanup_total` records confirmed cleanup outcomes.
These metrics contain no chat, file, user, URL, or credential labels. Configure the
gateway's OTEL exporter along with the other services when collecting them.
