"""The internal-only sandbox runner API (plan 14.2).

Exposure model (plan 24.1): this service lives on the compose ``runner``
network and is reachable only by the agent worker (plus an optional
127.0.0.1 binding in compose.dev.yaml for debugging). Every job endpoint
additionally requires the shared bearer token from ``SANDBOX_RUNNER_TOKEN``;
network isolation is the wall, the token is the lock on the door.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import TracebackType

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from jhin_observability import (
    ObservabilityRuntime,
    get_logger,
    initialize_observability,
    service_version,
)
from jhin_sandbox_runner.docker_socket import DockerSocketConfigurationError
from jhin_sandbox_runner.jobs import JobManager, JobValidationError
from jhin_sandbox_runner.schemas import (
    SandboxJobRequest,
    SandboxJobStatusResponse,
    SandboxLogsResponse,
)
from jhin_sandbox_runner.settings import Settings
from jhin_secrets.redaction import redact_event_dict

logger = get_logger(__name__)


def install_existing_runner_routes(
    app: FastAPI,
    active_settings: Settings,
    manager: JobManager,
) -> None:
    def require_token(request: Request) -> None:
        """Fail closed: no configured token means no access at all."""
        configured = active_settings.sandbox_runner_token
        header = request.headers.get("authorization", "")
        presented = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not configured or not presented or not secrets.compare_digest(presented, configured):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid runner token"
            )

    @app.get("/health")
    async def health() -> JSONResponse:
        docker_ok = await manager.ping()
        payload = {
            "status": "ok" if docker_ok else "unavailable",
            "docker": docker_ok,
        }
        return JSONResponse(
            status_code=(status.HTTP_200_OK if docker_ok else status.HTTP_503_SERVICE_UNAVAILABLE),
            content=payload,
        )

    @app.post(
        "/v1/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    async def submit_job(request: SandboxJobRequest) -> SandboxJobStatusResponse:
        try:
            record = await manager.submit(request)
        except JobValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        return record.to_response()

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    async def job_status(job_id: str) -> SandboxJobStatusResponse:
        record = manager.get(job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        return record.to_response()

    @app.get("/v1/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
    async def job_logs(job_id: str) -> SandboxLogsResponse:
        record = manager.get(job_id)
        logs = await manager.current_logs(job_id)
        if record is None or logs is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        stdout, stderr, stdout_truncated, stderr_truncated = logs
        return SandboxLogsResponse(
            job_id=job_id,
            status=record.status,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_job(job_id: str) -> SandboxJobStatusResponse:
        record = await manager.cancel(job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        return record.to_response()

    @app.delete(
        "/v1/workspaces/{workspace_key}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_token)],
    )
    async def delete_workspace(workspace_key: str) -> None:
        await manager.delete_workspace(workspace_key)


async def _close_manager_and_runtime(
    manager: JobManager,
    runtime: ObservabilityRuntime,
    *,
    owns_runtime: bool,
) -> BaseException | None:
    first_cancellation: asyncio.CancelledError | None = None
    first_error: BaseException | None = None

    def remember(error: BaseException) -> None:
        nonlocal first_cancellation, first_error
        if isinstance(error, asyncio.CancelledError):
            if first_cancellation is None:
                first_cancellation = error
        elif first_error is None:
            first_error = error

    try:
        await manager.close()
    except BaseException as error:
        remember(error)
    if owns_runtime:
        try:
            runtime.shutdown(timeout_millis=5_000)
        except BaseException as error:
            remember(error)
    return first_cancellation or first_error


async def _await_cleanup(
    task: asyncio.Task[BaseException | None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            if task.done():
                return task.result(), cancellation


def create_app(
    settings: Settings | None = None,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> FastAPI:
    active_settings = settings if settings is not None else Settings()
    owns_runtime = runtime is None
    if runtime is None:
        active_runtime = initialize_observability(
            active_settings.observability_config(
                service_name="sandbox-runner",
                service_version=service_version("jhin-sandbox-runner"),
                extra_log_processors=(redact_event_dict,),
            )
        )
    else:
        active_runtime = runtime
    try:
        manager = JobManager(active_settings)

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            del app
            active_error: BaseException | None = None
            active_traceback: TracebackType | None = None
            try:
                try:
                    await manager.start()
                except DockerSocketConfigurationError as refusal:
                    # Still fatal; only visible. uvicorn reports a failed
                    # startup through its stdlib logger, whose text the log
                    # contract replaces with a constant, so an operator who
                    # mis-set the Docker authority would otherwise get a
                    # non-zero exit and no reason. The event registry decides
                    # which of the runner's own sentences may be carried.
                    logger.error(
                        "sandbox_runner.docker_authority_refused",
                        reason=str(refusal),
                    )
                    raise
                logger.info(
                    "sandbox_runner.started",
                    token_configured=bool(active_settings.sandbox_runner_token),
                )
                yield
            except BaseException as error:
                active_error = error
                active_traceback = error.__traceback__
            cleanup_task = asyncio.create_task(
                _close_manager_and_runtime(
                    manager,
                    active_runtime,
                    owns_runtime=owns_runtime,
                ),
                name="sandbox-runner-cleanup",
            )
            cleanup_error, cleanup_cancellation = await _await_cleanup(cleanup_task)
            if isinstance(active_error, asyncio.CancelledError):
                raise active_error.with_traceback(active_traceback)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if active_error is not None:
                raise active_error.with_traceback(active_traceback)
            if cleanup_error is not None:
                raise cleanup_error

        app = FastAPI(
            title="Jhin Sandbox Runner",
            lifespan=lifespan,
            docs_url=None,
            redoc_url=None,
        )
        app.state.observability = active_runtime
        app.state.manager = manager
        install_existing_runner_routes(app, active_settings, manager)
        return app
    except BaseException:
        if owns_runtime:
            with suppress(BaseException):
                active_runtime.shutdown(timeout_millis=5_000)
        raise


def main() -> None:
    settings = Settings()
    runtime = initialize_observability(
        settings.observability_config(
            service_name="sandbox-runner",
            service_version=service_version("jhin-sandbox-runner"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    active_error: BaseException | None = None
    active_traceback: TracebackType | None = None
    try:
        uvicorn.run(
            create_app(settings, runtime=runtime),
            host="0.0.0.0",  # internal network only; never published to the host
            port=settings.sandbox_runner_port,
            log_config=None,
        )
    except BaseException as error:
        active_error = error
        active_traceback = error.__traceback__
    try:
        runtime.shutdown(timeout_millis=5_000)
    except BaseException:
        if active_error is None:
            raise
    if active_error is not None:
        raise active_error.with_traceback(active_traceback)


run = main


if __name__ == "__main__":
    main()
