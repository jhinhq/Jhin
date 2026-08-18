"""Webhook ingress endpoint (plan 19).

Deliberately NOT session-authenticated and NOT CSRF-protected: providers
call it machine-to-machine. Authentication is the unguessable public
connection id in the path plus mandatory HMAC signature verification inside
the service (plan 48.5). Always returns 202 for verified deliveries —
including duplicates and ignored event types — so providers don't retry.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from jhin_api.deps import DbSession, JetStreamDep, SecretCryptoDep
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.webhooks import service

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

MAX_WEBHOOK_BODY_BYTES = service.MAX_WEBHOOK_BODY_BYTES


def parse_optional_nonnegative_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def read_bounded_body(request: Request) -> bytes:
    content_length = parse_optional_nonnegative_content_length(request)
    if content_length is not None and content_length > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook body is too large",
        )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body is too large",
            )
        body.extend(chunk)
    return bytes(body)


class WebhookAck(BaseModel):
    status: str
    event_id: str | None = None


@router.post("/{connector_type}/{public_id}", status_code=202)
async def receive_webhook(
    connector_type: str,
    public_id: str,
    request: Request,
    db: DbSession,
    crypto: SecretCryptoDep,
    js: JetStreamDep,
) -> WebhookAck:
    body = await read_bounded_body(request)
    result = await service.process_delivery(
        db,
        crypto,
        js,
        connector_type=connector_type,
        public_id=public_id,
        headers=request.headers,
        body=body,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return WebhookAck(
        status=result.outcome,
        event_id=str(result.event_id) if result.event_id else None,
    )
