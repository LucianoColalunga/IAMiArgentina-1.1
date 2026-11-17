"""API de la consola de operadores (OIDC + RBAC). Cada acción queda auditada."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from iamia.api.deps import container, current_principal
from iamia.channels.models import Channel
from iamia.core.errors import Forbidden
from iamia.messaging.contracts import Job
from iamia.security.rbac import Permission, Principal

router = APIRouter(prefix="/admin/api", tags=["administración"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4000)


@router.get("/me")
async def me(principal: PrincipalDep) -> dict[str, Any]:
    return {
        "subject": principal.subject,
        "name": principal.name,
        "roles": sorted(role.value for role in principal.roles),
        "permissions": sorted(permission.value for permission in principal.permissions),
    }


@router.get("/conversations")
async def list_conversations(
    request: Request, principal: PrincipalDep, state: Annotated[list[str] | None, Query()] = None
) -> dict[str, Any]:
    items = await container(request).console.list(principal, state or ["waiting_human", "human"])
    return {"items": items}


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: uuid.UUID, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return await container(request).console.detail(principal, str(conversation_id))


@router.post("/conversations/{conversation_id}/claim", status_code=204)
async def claim(conversation_id: uuid.UUID, request: Request, principal: PrincipalDep) -> None:
    await container(request).console.claim(principal, str(conversation_id))


@router.post("/conversations/{conversation_id}/reply", status_code=202)
async def reply(
    conversation_id: uuid.UUID, body: ReplyRequest, request: Request, principal: PrincipalDep
) -> dict[str, str]:
    message_id = await container(request).console.reply(principal, str(conversation_id), body.text)
    return {"message_id": message_id}


@router.post("/conversations/{conversation_id}/release", status_code=204)
async def release(conversation_id: uuid.UUID, request: Request, principal: PrincipalDep) -> None:
    await container(request).console.release(principal, str(conversation_id))


@router.post("/conversations/{conversation_id}/close", status_code=204)
async def close(conversation_id: uuid.UUID, request: Request, principal: PrincipalDep) -> None:
    await container(request).console.close(principal, str(conversation_id))


@router.delete("/conversations/{conversation_id}/contact", status_code=204)
async def erase_contact(conversation_id: uuid.UUID, request: Request, principal: PrincipalDep) -> None:
    await container(request).console.erase_contact(principal, str(conversation_id))


@router.get("/problems")
async def problems(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": await container(request).console.problems(principal)}


@router.post("/problems/{message_id}/retry", status_code=202)
async def retry(message_id: uuid.UUID, request: Request, principal: PrincipalDep) -> None:
    await container(request).console.retry(principal, str(message_id))


@router.get("/channels")
async def channels(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    app = container(request)
    summary = await app.console.summary(principal)
    queues = {}
    for channel in app.registry.channels:
        queues[channel.value] = await app.queue.depth(Job("outbound", "", channel.value).routing_key)
    queues["inbound"] = await app.queue.depth("inbound")
    return {
        "enabled": [channel.value for channel in app.registry.channels],
        "available": [channel.value for channel in Channel],
        "queues": queues,
        "last_24h": summary,
    }


@router.get("/audit")
async def audit(
    request: Request, principal: PrincipalDep, before: int | None = None, limit: int = 100
) -> dict[str, Any]:
    if not principal.can(Permission.AUDIT_READ):
        raise Forbidden("audit:read")
    return {"items": await container(request).audit.list(limit=limit, before_id=before)}
