"""Webhook ingress endpoint (plan 19).

Deliberately NOT session-authenticated and NOT CSRF-protected: providers
call it machine-to-machine. Authentication is the unguessable public
connection id in the path plus mandatory HMAC signature verification inside
the service (plan 48.5). Always returns 202 for verified deliveries —
including duplicates and ignored event types — so providers don't retry.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from jhin_api.deps import DbSession, JetStreamDep, SecretCryptoDep
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.webhooks import service

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


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
    body = await request.body()
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
