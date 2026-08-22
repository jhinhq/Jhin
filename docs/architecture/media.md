# Agent avatars and media

Jhin agents present one of three avatars: accessible **initials** (the default
and the fallback), an **uploaded** raster image, or a **generated** stylized
illustration. Every stored avatar passes through one safe normalization
pipeline; originals are never kept, remote URLs are never fetched, and a
replacement only becomes active once every variant has validated. A generated
avatar is deliberately stylized and is never proof of identity.

## Components

| Piece | Location | Responsibility |
| --- | --- | --- |
| Normalizer | `packages/media/src/jhin_media/images.py` | Decode, validate, strip, crop, re-encode. |
| `MediaStore` | `packages/media/src/jhin_media/base.py` | Narrow storage boundary (`put_avatar`, `get_variant`, `retire`). |
| `PostgresMediaStore` | `packages/media/src/jhin_media/postgres.py` | V1 store: variants inline in `media_asset` (`bytea`), backup-safe for single-node installs. |
| Activation | `packages/media/src/jhin_media/avatars.py` | `activate_avatar` / `clear_avatar`: lock agent, stage asset, swap pointer, retire previous, flush (caller commits). |
| Prompt builder | `packages/media/src/jhin_media/prompts.py` | Builds generation prompts from public identity + explicit hint only. |
| Image capability | `packages/models/src/jhin_models/images.py` | Optional `ImageGenerationClient` protocol; `as_image_generation_client` raises `ImageGenerationUnsupported` for chat-only adapters. OpenAI-compatible adapters implement `POST /images/generations` with inline `b64_json`. |
| Workflow | `packages/workflows/src/jhin_workflows/avatar_generation/` | `AvatarGenerationWorkflow` (id `avatar-generation-<generation_id>`, agent task queue). |
| Activities | `services/agent_worker/src/jhin_agent_worker/media_activities.py` | `generate_avatar`, `fail_avatar_generation`. |
| API | `apps/api/src/jhin_api/media/` | Upload, remove, delivery, generation request/status. |

## Data model (migration `0017`, revises `0016`)

- `media_asset`: `workspace_id`, `kind` (`avatar`), `owner_agent_id`
  (composite FK to the agent, cascade), `status`
  (`pending|active|rejected|retired`), `content_type` (`image/webp`),
  `width`/`height` (256), `sha256` (hex over the three variants; the ETag
  source), `variant_64`/`variant_128`/`variant_256` (`bytea`),
  `created_by_user_id`, `retired_at`, timestamps.
- `avatar_generation`: `workspace_id`, `agent_id`, `prompt`, `prompt_hint`,
  `provider_type`, `provider_display_name`, `model_profile_id`, `model_name`,
  `image_size`, `estimated_cost_micros`, `status`
  (`queued|running|succeeded|failed`), `error`, `error_code`,
  `result_asset_id`, `temporal_workflow_id`, `created_by_user_id`,
  `started_at`, `finished_at`, timestamps.
- `agent.avatar_kind` (`initials|upload|generated`, default `initials`) and
  `agent.active_avatar_asset_id` (nullable, `ON DELETE SET NULL`).

## Normalization contract and limits

Input must be bytes the caller already holds (multipart upload or a provider's
inline base64). `normalize_avatar(data, declared_content_type=...)`:

| Rule | Limit / behaviour | Rejection code |
| --- | --- | --- |
| Byte size | ≤ 8 MB (`MAX_UPLOAD_BYTES`); the route reads one byte past the limit and answers 413 | `too_large` |
| Decoded format | PNG, JPEG, WebP only (Pillow-decoded; SVG, GIF, video, unknown → rejected) | `unsupported_format` |
| Declared MIME | Must match the decoded format when provided | `content_type_mismatch` |
| Animation | `is_animated` or `n_frames > 1` rejected (GIF/APNG/animated WebP) | `animated` |
| Pixels | ≤ 4096×4096 (`MAX_DIMENSION`), ≥ 16 px on each side | `too_many_pixels`, `too_small` |
| Decompression bombs | `Image.MAX_IMAGE_PIXELS` set to 4096²; the bomb warning is an error | `decompression_bomb` |
| Truncated / corrupt | Full pixel load must succeed | `undecodable`, `empty` |
| Metadata | EXIF orientation applied, then pixels copied into a fresh image: no EXIF/ICC/XMP/comments survive | — |
| Output | Center-crop to square, LANCZOS resize, WebP q85 at 64/128/256 px | — |

Variants are content-addressed by asset id: every replacement gets a new id,
so media responses can be cached privately for a day.

## Endpoints

All routes live under `/api/v1/workspaces/{workspace_id}` and scope every
query by workspace; an asset or agent from another workspace is `404`.
Mutating routes require the workspace `admin` role and the CSRF header.

### `GET /agents/{agent_id}/avatar` (viewer)

```json
{
  "agent_id": "…", "workspace_id": "…",
  "avatar_kind": "upload",
  "active_avatar_asset_id": "…",
  "avatar_url": "/api/v1/workspaces/…/media/…",
  "initials": "AL"
}
```

### `POST /agents/{agent_id}/avatar` (admin, multipart field `file`) → `201 AvatarOut`

Validates and activates atomically; the previous asset is retired in the same
transaction. Errors: `413 {code: too_large}`, `422 {code: <rejection code>,
message}`. Audit: `agent.avatar.uploaded`.

### `DELETE /agents/{agent_id}/avatar` (admin) → `200 AvatarOut`

Returns the agent to initials and retires the asset; idempotent. Audit:
`agent.avatar.removed`.

### `GET /media/{asset_id}?size=64|128|256` (viewer; default 128)

Returns the WebP bytes with `Content-Type: image/webp`,
`Cache-Control: private, max-age=86400, immutable`, `ETag`,
`X-Content-Type-Options: nosniff`, an inline `Content-Disposition`, and a
sandboxing CSP. Honours `If-None-Match` with `304`. Retired, rejected,
pending, or foreign assets are `404`. `size` outside the set is `422`.

### `POST /agents/{agent_id}/avatar/generate` (admin) → `202 AvatarGenerationOut`

Body: `{"prompt_hint": "optional, ≤ 300 chars"}`.

The service picks an image-capable model profile — the agent's profile, then
the workspace default, then any enabled profile in the workspace — whose
`config_json.image_generation` declares
`{"enabled": true, "model": "<image model>", "size": "1024x1024", "cost_micros": <int|null>}`
and whose provider is enabled. Without one the response is
`409 {code: image_generation_unsupported}`; a queued/running generation for the
same agent is `409 {code: generation_in_progress}`.

The prompt is built from **public identity only** (name, role title, public
purpose, expertise tags) plus the hint; system prompts, memory, and
conversations never reach it. The row is committed before the workflow starts
so dispatch failures are visible (`503`, row marked `failed` with
`error_code = dispatch_failed`). Audit: `agent.avatar.generation_requested`.

```json
{
  "id": "…", "workspace_id": "…", "agent_id": "…",
  "status": "queued",
  "prompt": "Stylized editorial illustration portrait avatar, … Subject: an AI teammate called Ada …",
  "prompt_hint": "warm colors",
  "disclosure": {
    "provider_type": "openai",
    "provider_display_name": "OpenAI",
    "model_profile_id": "…",
    "model_name": "gpt-image-1",
    "image_size": "1024x1024",
    "estimated_cost_micros": 40000,
    "sends_public_identity": true
  },
  "error": null, "error_code": null,
  "result_asset_id": null, "result_avatar_url": null,
  "temporal_workflow_id": "avatar-generation-…",
  "created_at": "…", "started_at": null, "finished_at": null, "updated_at": "…"
}
```

The UI shows `disclosure` (provider, model, estimated cost) before and while
the generation runs.

### `GET /agents/{agent_id}/avatar/generation` (viewer)

Latest generation for the agent as `AvatarGenerationOut`; `404` when none.
Terminal statuses are `succeeded` (with `result_asset_id` /
`result_avatar_url`) and `failed` (with user-safe `error_code` / `error`).

### Agent responses

`AgentOut` gains `avatar_kind`, `active_avatar_asset_id`, and `avatar_url`
(relative media path or `null`). Creation and updates do not accept avatar
fields: new agents start as `initials` and avatar changes go through the
routes above, so agent creation never waits on generation.

## Generation workflow

1. API commits `avatar_generation(status=queued)` and starts
   `AvatarGenerationWorkflow` with id `avatar-generation-<id>`
   (`WorkflowAlreadyStartedError` is treated as success).
2. `generate_avatar` (agent worker; 3 attempts with backoff for retryable
   provider errors) marks the row `running`, resolves the profile/provider,
   decrypts the credential at the moment of use, calls
   `generate_image(prompt, model, size)`, normalizes the bytes with the same
   pipeline as uploads, and activates the asset atomically
   (`agent.avatar.generated`, actor `system`). A committed success is
   idempotent on retry.
3. On any terminal failure (unsupported profile, provider error, rejected
   image, missing agent) the workflow runs `fail_avatar_generation`, which
   records `status=failed`, `error_code`, a redacted `error`, and
   `agent.avatar.generation_failed`. The agent's previous avatar stays active.

Error codes surfaced to the UI: `image_generation_unsupported`,
`provider_disabled`, `provider_config`, `provider_error`, `image_rejected`,
`agent_not_found`, `generation_not_found`, `dispatch_failed`.

## Audit events

`agent.avatar.uploaded`, `agent.avatar.removed`,
`agent.avatar.generation_requested`, `agent.avatar.generated`,
`agent.avatar.generation_failed`.

## Testing

- `packages/media/tests/test_images.py`: format acceptance, variants, metadata
  stripping, SVG/GIF/animated/mismatch/oversized/bomb/truncated rejection.
- `packages/models/tests/test_image_generation.py`: OpenAI-compatible images
  adapter against the fake server (deterministic PNG), unsupported providers.
- `apps/api/tests/test_media_unit.py`: atomic activation, retirement, 404s,
  generation dispatch/disclosure, HTTP delivery headers, RBAC and CSRF.
- `packages/workflows/tests/test_avatar_generation_workflow.py` and
  `services/agent_worker/tests/test_media_activities.py`: success activation;
  failures keep the previous avatar.

The fake provider (`jhin_models.testing.FakeOpenAIServer`) serves
`POST /v1/images/generations` with a deterministic tiny PNG so the whole
pipeline runs without external services.
