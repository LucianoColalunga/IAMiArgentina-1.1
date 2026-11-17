"""Acciones de operadores sobre conversaciones derivadas, con permisos y auditoría."""

from __future__ import annotations

import time
from typing import Any

from iamia.channels.models import OutboundContent
from iamia.conversations import texts
from iamia.conversations.models import Author, OutboundDraft
from iamia.core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from iamia.messaging.contracts import Job
from iamia.persistence.console import ConsoleRepository
from iamia.persistence.contacts import AuditRepository
from iamia.persistence.work import WorkRepository
from iamia.security.input import sanitize_user_input
from iamia.security.rbac import Permission, Principal
from iamia.services.publisher import JobPublisher

MAX_REPLY_CHARS = 4000
# La consola refresca la conversación abierta cada pocos segundos: se audita la primera vista de
# cada operador por conversación en esta ventana, no cada refresco.
VIEW_AUDIT_WINDOW_SECONDS = 600.0
_VIEW_CACHE_LIMIT = 10_000


class ConsoleService:
    def __init__(
        self,
        console: ConsoleRepository,
        work: WorkRepository,
        audit: AuditRepository,
        publisher: JobPublisher,
    ) -> None:
        self._console = console
        self._viewed: dict[tuple[str, str], float] = {}
        self._work = work
        self._audit = audit
        self._publisher = publisher

    @staticmethod
    def _require(principal: Principal, permission: Permission) -> None:
        if not principal.can(permission):
            raise Forbidden(f"falta el permiso {permission.value}")

    async def list(self, principal: Principal, states: list[str]) -> list[dict[str, Any]]:
        self._require(principal, Permission.CONVERSATIONS_READ)
        allowed = {"waiting_human", "human", "bot", "closed"}
        return await self._console.list_conversations([s for s in states if s in allowed] or ["waiting_human"])

    async def detail(self, principal: Principal, conversation_id: str) -> dict[str, Any]:
        self._require(principal, Permission.CONVERSATIONS_READ)
        data = await self._console.conversation(conversation_id)
        if data is None:
            raise NotFound("la conversación no existe")
        if self._first_view(principal.subject, conversation_id):
            await self._audit.record(
                principal.subject, "conversation_viewed", target_type="conversation", target_id=conversation_id
            )
        return data

    def _first_view(self, subject: str, conversation_id: str) -> bool:
        now = time.monotonic()
        if len(self._viewed) >= _VIEW_CACHE_LIMIT:
            self._viewed = {k: at for k, at in self._viewed.items() if now - at < VIEW_AUDIT_WINDOW_SECONDS}
        last = self._viewed.get((subject, conversation_id))
        if last is not None and now - last < VIEW_AUDIT_WINDOW_SECONDS:
            return False
        self._viewed[(subject, conversation_id)] = now
        return True

    async def claim(self, principal: Principal, conversation_id: str) -> None:
        self._require(principal, Permission.CONVERSATIONS_CLAIM)
        can_reassign = principal.can(Permission.CONVERSATIONS_REASSIGN)
        if not await self._console.claim(conversation_id, principal.subject, can_reassign=can_reassign):
            raise Conflict("la conversación está asignada a otra persona o está cerrada")
        await self._audit.record(
            principal.subject, "conversation_claimed", target_type="conversation", target_id=conversation_id
        )

    async def reply(self, principal: Principal, conversation_id: str, text: str) -> str:
        self._require(principal, Permission.CONVERSATIONS_REPLY)
        clean = sanitize_user_input(text)
        if not clean or len(clean) > MAX_REPLY_CHARS:
            raise InvalidRequest(f"la respuesta debe tener entre 1 y {MAX_REPLY_CHARS} caracteres")
        current = await self._console.assigned_to(conversation_id)
        if current is None:
            raise NotFound("la conversación no existe")
        state, assignee = current
        if state != "human" or assignee != principal.subject:
            raise Conflict("primero hay que tomar la conversación")
        draft = OutboundDraft(OutboundContent(clean), author=Author.OPERATOR, operator=principal.subject)
        created = await self._work.add_outbound(conversation_id, draft)
        if created is None:
            raise NotFound("la conversación no existe")
        message_id, channel = created
        await self._publisher.publish([Job("outbound", message_id, channel.value)])
        await self._audit.record(
            principal.subject,
            "conversation_replied",
            target_type="conversation",
            target_id=conversation_id,
            detail={"message_id": message_id},
        )
        return message_id

    async def release(self, principal: Principal, conversation_id: str) -> None:
        """Devuelve la conversación al asistente automático y se lo avisa a la persona."""
        self._require(principal, Permission.CONVERSATIONS_CLOSE)
        operator = "*" if principal.can(Permission.CONVERSATIONS_REASSIGN) else principal.subject
        if not await self._console.set_state(conversation_id, "bot", operator=operator, reason="released"):
            raise Conflict("no se pudo devolver la conversación al asistente")
        await self._notify(conversation_id, texts.HANDOFF_RELEASED)
        await self._audit.record(
            principal.subject, "conversation_released", target_type="conversation", target_id=conversation_id
        )

    async def close(self, principal: Principal, conversation_id: str) -> None:
        self._require(principal, Permission.CONVERSATIONS_CLOSE)
        operator = "*" if principal.can(Permission.CONVERSATIONS_REASSIGN) else principal.subject
        if not await self._console.set_state(conversation_id, "closed", operator=operator, reason="operator"):
            raise Conflict("no se pudo cerrar la conversación")
        await self._audit.record(
            principal.subject, "conversation_closed", target_type="conversation", target_id=conversation_id
        )

    async def _notify(self, conversation_id: str, text: str) -> None:
        created = await self._work.add_outbound(conversation_id, OutboundDraft(OutboundContent(text), Author.SYSTEM))
        if created is not None:
            message_id, channel = created
            await self._publisher.publish([Job("outbound", message_id, channel.value)])

    async def problems(self, principal: Principal) -> list[dict[str, Any]]:
        self._require(principal, Permission.DEAD_LETTERS_MANAGE)
        return await self._console.problems()

    async def retry(self, principal: Principal, message_id: str) -> None:
        self._require(principal, Permission.DEAD_LETTERS_MANAGE)
        channel = await self._console.retry_message(message_id)
        if channel is None:
            raise NotFound("el mensaje no existe o no está fallido")
        await self._publisher.publish([Job("outbound", message_id, channel.value)])
        await self._audit.record(principal.subject, "message_retried", target_type="message", target_id=message_id)

    async def erase_contact(self, principal: Principal, conversation_id: str) -> None:
        self._require(principal, Permission.CONTACTS_DELETE)
        if not await self._console.erase_contact_of(conversation_id):
            raise NotFound("la conversación no existe")
        await self._audit.record(
            principal.subject, "contact_erased", target_type="conversation", target_id=conversation_id
        )

    async def summary(self, principal: Principal) -> list[dict[str, Any]]:
        self._require(principal, Permission.CHANNELS_READ)
        return await self._console.channel_summary()
