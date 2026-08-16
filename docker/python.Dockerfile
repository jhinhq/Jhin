# Shared multi-stage image for all Jhin Python services.
# Build with: --build-arg SERVICE_PACKAGE=<jhin-api|jhin-workflow-worker|jhin-event-worker>
# The service command is set per-service in compose.yaml.

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Resolve third-party dependencies first so they cache across source changes.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/
COPY services/workflow_worker/pyproject.toml services/workflow_worker/
COPY services/event_worker/pyproject.toml services/event_worker/
COPY packages/db/pyproject.toml packages/db/
COPY packages/events/pyproject.toml packages/events/
COPY packages/workflows/pyproject.toml packages/workflows/
COPY packages/observability/pyproject.toml packages/observability/

ARG SERVICE_PACKAGE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-workspace --package "${SERVICE_PACKAGE}"

COPY apps ./apps
COPY packages ./packages
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${SERVICE_PACKAGE}"

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
RUN useradd --create-home --uid 10001 jhin
COPY --from=builder --chown=jhin:jhin /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER jhin
WORKDIR /home/jhin
