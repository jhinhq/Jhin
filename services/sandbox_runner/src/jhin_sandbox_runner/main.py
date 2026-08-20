"""The internal-only sandbox runner API (plan 14.2).

Exposure model (plan 24.1): this service lives on the compose ``runner``
network and is reachable only by the agent worker (plus an optional
127.0.0.1 binding in compose.dev.yaml for debugging). Every job endpoint
additionally requires the shared bearer token from ``SANDBOX_RUNNER_TOKEN``;
network isolation is the wall, the token is the lock on the door.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from jhin_observability import configure_json_logging, get_logger, normalize_environment
from jhin_sandbox_runner.jobs import JobManager, JobValidationError
from jhin_sandbox_runner.schemas import (
    SandboxJobRequest,
    SandboxJobStatusResponse,
    SandboxLogsResponse,
)
from jhin_sandbox_runner.settings import Settings

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings if settings is not None else Settings()
    manager = JobManager(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        logger.info(
            "sandbox_runner.started",
            token_configured=bool(active_settings.sandbox_runner_token),
        )
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Jhin Sandbox Runner", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager

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

    return app


def main() -> None:
    settings = Settings()
    configure_json_logging(
        service="sandbox-runner",
        environment=normalize_environment(settings.app_env),
        level=settings.log_level,
    )
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # internal network only; never published to the host
        port=settings.sandbox_runner_port,
        log_config=None,
    )


run = main


if __name__ == "__main__":
    main()
