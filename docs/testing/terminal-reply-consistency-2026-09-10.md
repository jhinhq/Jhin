# Terminal reply consistency — September 10, 2026

The reported Python-to-Vite mismatch came from an unsupported first answer.
Task `01a08d04-d8fb-7b51-9508-608a8ae917b5` claimed a Python directory listing
with zero tool calls. Follow-up `01a08d05-7466-77e1-ab5c-16050b35e206`
actually called `cli.file.read` and `cli.file.list`, observing a Vite checkout.
There was no evidence of a sandbox switch between those requests.

A fresh test against deployed prompt version 17 exposed a remaining gap:
`01a08e59-acfd-7780-a414-35180852b4e2` listed the real files, but follow-up
`01a08e5a-7714-7ef1-a20e-92a4e8562c0b` claimed the file was absent without
another observation. Prompt guidance alone did not reliably correct it.

The agent worker now reviews an unpublished tool-free draft once when tools
are available but there is no tool result after the latest request. Earlier
chat tasks and earlier requests within the same task do not satisfy that check.
The review asks for current observations before file, absence, and command
claims; ordinary answers can still remain tool-free. The draft is redacted,
bounded to 4,000 characters, JSON-quoted and marked untrusted. This shares one
retry budget with empty-response recovery, preserves usage accounting, and
uses the existing manifest, authorization and replay paths.

This is bounded model recovery, not a semantic truth guarantee. It adds one
generation for eligible tool-free drafts. An unrelated tool result or an
unsupported second draft can still require further evaluation.

## Verification

- 36 reasoning/manifest tests passed, including draft-to-read recovery,
  ordinary replies, failed reads, instruction boundaries, retry bounds,
  accounting and replay. An independent focused review passed 14 cases.
- Adjacent history, sequencing, legacy-sidecar and telemetry checks passed
  across focused runs. Three sequencing fixtures needed an extra canned
  response for the review pass; their original assertions remain intact.
- Ruff, formatting, targeted mypy, diff checks and the agent-worker Docker
  build passed. The updated worker is deployed and healthy; no migration.
- Live Ollama task `01a08e63-c1de-7c23-8ec0-d0d23ec2ed94` listed real Vite files.
- Follow-up `01a08e64-68d7-7e00-96db-640d19746b56` performed new file operations
  before answering. Its depth-five listing was untruncated and contained no
  `requirements.txt`. The content-search call alone is not proof of filename
  absence; the listing supplies that evidence within its depth bound.
- Terminal task `01a08e65-8cbd-7170-9275-de0ad01e6232` executed `pwd && ls -la`
  through `cli.test.run`, exit 0, with untruncated stdout. Its reply reproduced
  the actual output and identified `/workspace/repo`.
- Repeating the file question in the formerly failing test conversation,
  task `01a08e66-d8a4-7dc2-9023-798cdde56dbf`, made new list/search calls too.
  Both temporary verification chats were archived after completion; the
  user's original conversation and its recorded replies were preserved.

These live turns called tools directly; their success alone does not prove
the review branch ran. Branch behavior is separately covered by the activity
regressions. Detailed local evidence is in `.tmp/live-agent-20260910/` and is
not intended for publication. No credentials are saved in those fixtures.
