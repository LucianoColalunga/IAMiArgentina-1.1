"""Gestor de conversaciones: decide qué hacer con cada mensaje entrante.

Orden de decisión:
1. Baja (opt-out): si la persona pidió no recibir mensajes, solo se atiende ALTA.
2. Comandos (BAJA, HUMANO, BORRAR, VINCULAR, ...).
3. Derivación: si una persona del equipo tiene la conversación, el bot no responde.
4. Límite por contacto (anti-spam y anti-bucles entre bots).
5. Contenido no textual: se avisa que solo se lee texto.
6. Contexto: preguntas de seguimiento se reescriben como preguntas completas.
7. Motor RAG. Tras varias consultas sin información se ofrece hablar con una persona.

No guarda nada por sí mismo: devuelve una Decision que el worker persiste en una transacción.
Los errores transitorios del motor (ServiceBusy, ServiceUnavailable) se propagan para que el
worker reintente con backoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from iamia.ai.engine import AssistantEngine, Outcome
from iamia.channels.models import MessageKind, OutboundContent, SourceLink
from iamia.conversations import texts
from iamia.conversations.commands import Command, ParsedCommand, format_link_code, new_link_code, parse_command
from iamia.conversations.context import QueryRewriter
from iamia.conversations.models import (
    ConversationState,
    Decision,
    InboundStatus,
    InboundWork,
    OutboundDraft,
)
from iamia.conversations.ports import AuditTrail, CodeHasher, ContactCommands, ContactThrottle
from iamia.core.errors import InvalidRequest, QuotaExceeded
from iamia.core.metrics import HANDOFFS

logger = logging.getLogger(__name__)

_SILENT_KINDS = {MessageKind.REACTION, MessageKind.STICKER}
_HUMAN_STATES = {ConversationState.WAITING_HUMAN, ConversationState.HUMAN}


@dataclass(frozen=True)
class ManagerConfig:
    handoff_enabled: bool = True
    handoff_after_no_info: int = 2
    identity_linking_enabled: bool = True
    link_code_ttl_seconds: int = 600
    retention_messages_days: int = 90


def _reply(text: str, sources: tuple[SourceLink, ...] = ()) -> OutboundDraft:
    return OutboundDraft(OutboundContent(text, sources))


class ConversationManager:
    def __init__(
        self,
        *,
        engine: AssistantEngine,
        rewriter: QueryRewriter,
        contacts: ContactCommands,
        audit: AuditTrail,
        throttle: ContactThrottle,
        code_hasher: CodeHasher,
        config: ManagerConfig | None = None,
    ) -> None:
        self._engine = engine
        self._rewriter = rewriter
        self._contacts = contacts
        self._audit = audit
        self._throttle = throttle
        self._hash_code = code_hasher
        self._config = config or ManagerConfig()

    async def handle(self, work: InboundWork) -> Decision:
        text = (work.text or "").strip()
        parsed = parse_command(text) if work.kind.has_text else None

        if work.contact.opted_out:
            if parsed and parsed.command in (Command.RESUME, Command.START):
                return await self._resume(work)
            return Decision(InboundStatus.IGNORED, "opted_out")
        if parsed and parsed.command in (Command.STOP, Command.RESUME):
            return await self._command(parsed, work)  # la baja funciona siempre, sin límite
        conversational = not parsed or parsed.command is Command.GREETING
        if conversational and work.conversation.state in _HUMAN_STATES:
            return Decision(InboundStatus.IGNORED, "human_handling")  # lo ve el operador en la consola
        if not parsed and work.kind in _SILENT_KINDS:
            return Decision(InboundStatus.IGNORED, "silent_kind")

        allowed, notify = await self._throttle.check(work.contact.address_hash)
        if not allowed:
            replies = (_reply(texts.TOO_MANY_MESSAGES),) if notify else ()
            return Decision(InboundStatus.ANSWERED if notify else InboundStatus.IGNORED, "throttled", replies)
        if parsed:
            return await self._command(parsed, work)
        if not work.kind.has_text or not text:
            return Decision(InboundStatus.ANSWERED, "unsupported_content", (_reply(texts.UNSUPPORTED_CONTENT),))

        question = await self._rewriter.standalone(text, work.history)
        try:
            answer = await self._engine.answer(question, client_key=work.contact.address_hash[:32])
        except InvalidRequest as exc:
            return Decision(InboundStatus.ANSWERED, "invalid_request", (_reply(exc.user_message),))
        except QuotaExceeded as exc:
            return Decision(InboundStatus.ANSWERED, "quota_exceeded", (_reply(exc.user_message),))

        sources = tuple(SourceLink(source.title, source.url) for source in answer.sources)
        replies = [_reply(answer.text, sources)]
        no_info = work.conversation.no_info_count + 1 if answer.outcome is Outcome.NO_INFO else 0
        if self._config.handoff_enabled and no_info == self._config.handoff_after_no_info:
            replies.append(_reply(texts.HANDOFF_SUGGESTION))
        return Decision(
            InboundStatus.ANSWERED, answer.outcome.value, tuple(replies), no_info_count=no_info, answer=answer
        )

    # ------------------------------------------------------------------ comandos
    async def _command(self, parsed: ParsedCommand, work: InboundWork) -> Decision:
        command = parsed.command
        outcome = f"command:{command.value}"
        if command in (Command.START, Command.GREETING):
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.WELCOME),))
        if command is Command.HELP:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.HELP),))
        if command is Command.PRIVACY:
            notice = texts.PRIVACY.format(days=self._config.retention_messages_days)
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(notice),))
        if command is Command.HUMAN:
            return self._handoff(work, outcome)
        if command is Command.ERASE:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.ERASE_CONFIRMATION_REQUEST),))
        if command is Command.ERASE_CONFIRM:
            await self._contacts.request_erasure(work.contact.id)
            await self._audit.record(
                "system", "contact_erasure_requested", target_type="contact", target_id=work.contact.id
            )
            return Decision(
                InboundStatus.ANSWERED, outcome, (_reply(texts.ERASE_DONE),), state=ConversationState.CLOSED
            )
        if command is Command.STOP:
            await self._contacts.set_opt_out(work.contact.id, True)
            await self._audit.record("system", "contact_opted_out", target_type="contact", target_id=work.contact.id)
            return Decision(
                InboundStatus.ANSWERED, outcome, (_reply(texts.OPT_OUT_DONE),), state=ConversationState.CLOSED
            )
        if command is Command.RESUME:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.OPT_IN_DONE),))
        return await self._link(parsed, work, outcome)

    async def _resume(self, work: InboundWork) -> Decision:
        await self._contacts.set_opt_out(work.contact.id, False)
        await self._audit.record("system", "contact_opted_in", target_type="contact", target_id=work.contact.id)
        return Decision(
            InboundStatus.ANSWERED, "command:resume", (_reply(texts.OPT_IN_DONE),), state=ConversationState.BOT
        )

    def _handoff(self, work: InboundWork, outcome: str) -> Decision:
        if not self._config.handoff_enabled:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.HANDOFF_DISABLED),))
        if work.conversation.state in _HUMAN_STATES:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.HANDOFF_ALREADY),))
        HANDOFFS.labels(work.contact.channel.value, "user_request").inc()
        return Decision(
            InboundStatus.ANSWERED,
            outcome,
            (_reply(texts.HANDOFF_STARTED),),
            state=ConversationState.WAITING_HUMAN,
            handoff_reason="user_request",
        )

    async def _link(self, parsed: ParsedCommand, work: InboundWork, outcome: str) -> Decision:
        if not self._config.identity_linking_enabled:
            return Decision(InboundStatus.ANSWERED, outcome, (_reply(texts.LINK_DISABLED),))
        if parsed.argument:
            linked = await self._contacts.redeem_link_code(self._hash_code(parsed.argument), work.contact.id)
            if linked:
                await self._audit.record("system", "contacts_linked", target_type="contact", target_id=work.contact.id)
            return Decision(
                InboundStatus.ANSWERED, outcome, (_reply(texts.LINK_DONE if linked else texts.LINK_INVALID),)
            )
        code = new_link_code()
        ttl = self._config.link_code_ttl_seconds
        await self._contacts.create_link_code(work.contact.id, self._hash_code(code), ttl)
        message = texts.LINK_CODE.format(code=format_link_code(code), minutes=max(1, ttl // 60))
        return Decision(InboundStatus.ANSWERED, outcome, (_reply(message),))
