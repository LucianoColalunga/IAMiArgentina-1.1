"""Gestor de conversaciones: comandos, derivación, bajas, límites y contexto."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from iamia.ai.engine import Answer, Outcome, Source
from iamia.channels.models import MessageKind
from iamia.conversations import texts
from iamia.conversations.manager import ConversationManager, ManagerConfig
from iamia.conversations.models import ConversationState, InboundStatus
from iamia.core.errors import InvalidRequest, QuotaExceeded, ServiceBusy
from tests.factories import contact, conversation, inbound_work


@dataclass
class FakeEngine:
    answer_to_give: Answer = field(
        default_factory=lambda: Answer(
            "Podés tramitarlo en la app.", Outcome.ANSWERED, (Source("DNI", "https://www.argentina.gob.ar/dni"),)
        )
    )
    error: Exception | None = None
    questions: list[str] = field(default_factory=list)
    chat_model: Any = None

    async def answer(self, question: str, *, client_key: str) -> Answer:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.answer_to_give


class EchoRewriter:
    def __init__(self, rewritten: str | None = None) -> None:
        self.rewritten = rewritten

    async def standalone(self, question: str, history: Any) -> str:
        return self.rewritten or question


@dataclass
class FakeContacts:
    opt_outs: list[tuple[str, bool]] = field(default_factory=list)
    erasures: list[str] = field(default_factory=list)
    codes: dict[str, str] = field(default_factory=dict)
    redeem_ok: bool = True

    async def set_opt_out(self, contact_id: str, opted_out: bool) -> None:
        self.opt_outs.append((contact_id, opted_out))

    async def request_erasure(self, contact_id: str) -> None:
        self.erasures.append(contact_id)

    async def create_link_code(self, contact_id: str, code_hash: str, ttl_seconds: int) -> None:
        self.codes[code_hash] = contact_id

    async def redeem_link_code(self, code_hash: str, contact_id: str) -> bool:
        return self.redeem_ok and code_hash in self.codes


@dataclass
class FakeAudit:
    actions: list[str] = field(default_factory=list)

    async def record(self, actor: str, action: str, **_: Any) -> None:
        self.actions.append(action)


@dataclass
class FakeThrottle:
    allowed: bool = True
    notify: bool = False

    async def check(self, contact_key: str) -> tuple[bool, bool]:
        return self.allowed, self.notify


def build(engine: FakeEngine | None = None, **kwargs: Any) -> tuple[ConversationManager, FakeContacts, FakeAudit]:
    contacts, audit = FakeContacts(), FakeAudit()
    manager = ConversationManager(
        engine=engine or FakeEngine(),  # type: ignore[arg-type]
        rewriter=kwargs.pop("rewriter", EchoRewriter()),  # type: ignore[arg-type]
        contacts=contacts,
        audit=audit,
        throttle=kwargs.pop("throttle", FakeThrottle()),
        code_hasher=lambda code: f"h:{code}",
        config=kwargs.pop("config", ManagerConfig()),
    )
    return manager, contacts, audit


async def test_question_goes_to_engine_with_sources() -> None:
    engine = FakeEngine()
    manager, _, _ = build(engine)
    decision = await manager.handle(inbound_work("¿Cómo saco el DNI digital?"))
    assert decision.status is InboundStatus.ANSWERED
    assert decision.outcome == "answered"
    assert engine.questions == ["¿Cómo saco el DNI digital?"]
    reply = decision.replies[0].content
    assert reply.sources[0].url == "https://www.argentina.gob.ar/dni"
    assert decision.no_info_count == 0


async def test_follow_up_uses_rewritten_question() -> None:
    engine = FakeEngine()
    manager, _, _ = build(engine, rewriter=EchoRewriter("¿Cuánto cuesta el DNI?"))
    await manager.handle(inbound_work("¿y cuánto sale?"))
    assert engine.questions == ["¿Cuánto cuesta el DNI?"]


@pytest.mark.parametrize("text", ["HUMANO", "/humano", "hablar con una persona", "Operador"])
async def test_handoff_command(text: str) -> None:
    manager, _, _ = build()
    decision = await manager.handle(inbound_work(text))
    assert decision.state is ConversationState.WAITING_HUMAN
    assert decision.handoff_reason == "user_request"
    assert decision.replies[0].content.text == texts.HANDOFF_STARTED


async def test_handoff_disabled() -> None:
    manager, _, _ = build(config=ManagerConfig(handoff_enabled=False))
    decision = await manager.handle(inbound_work("humano"))
    assert decision.state is None
    assert decision.replies[0].content.text == texts.HANDOFF_DISABLED


async def test_messages_ignored_while_a_person_handles_the_conversation() -> None:
    engine = FakeEngine()
    manager, _, _ = build(engine)
    work = inbound_work("¿siguen ahí?", conversation=conversation(state=ConversationState.HUMAN))
    decision = await manager.handle(work)
    assert decision.status is InboundStatus.IGNORED
    assert decision.outcome == "human_handling"
    assert engine.questions == []


async def test_a_lone_greeting_gets_the_welcome_without_the_engine() -> None:
    engine = FakeEngine()
    manager, _, _ = build(engine)
    decision = await manager.handle(inbound_work("¡Hola!"))
    assert decision.outcome == "command:greeting"
    assert decision.replies[0].content.text == texts.WELCOME
    assert engine.questions == []


async def test_greetings_go_to_the_operator_and_do_not_undo_an_opt_out() -> None:
    manager, contacts, _ = build(FakeEngine())
    handled = await manager.handle(
        inbound_work("hola", conversation=conversation(state=ConversationState.WAITING_HUMAN))
    )
    assert handled.status is InboundStatus.IGNORED and handled.outcome == "human_handling"
    opted_out = await manager.handle(inbound_work("Buenas tardes", contact=contact(opted_out=True)))
    assert opted_out.status is InboundStatus.IGNORED and contacts.opt_outs == []


async def test_handoff_already_requested() -> None:
    manager, _, _ = build()
    work = inbound_work("humano", conversation=conversation(state=ConversationState.WAITING_HUMAN))
    decision = await manager.handle(work)
    assert decision.replies[0].content.text == texts.HANDOFF_ALREADY


async def test_stop_works_even_when_throttled_and_closes() -> None:
    manager, contacts, audit = build(throttle=FakeThrottle(allowed=False))
    decision = await manager.handle(inbound_work("BAJA"))
    assert contacts.opt_outs == [(inbound_work().contact.id, True)]
    assert decision.state is ConversationState.CLOSED
    assert "contact_opted_out" in audit.actions


async def test_opted_out_contact_is_ignored_until_resume() -> None:
    engine = FakeEngine()
    manager, contacts, _ = build(engine)
    opted_out = contact(opted_out=True)
    ignored = await manager.handle(inbound_work("¿Cómo saco el DNI?", contact=opted_out))
    assert ignored.status is InboundStatus.IGNORED and engine.questions == []
    resumed = await manager.handle(inbound_work("ALTA", contact=opted_out))
    assert contacts.opt_outs[-1] == (opted_out.id, False)
    assert resumed.state is ConversationState.BOT
    assert resumed.replies[0].content.text == texts.OPT_IN_DONE


async def test_erase_needs_confirmation() -> None:
    manager, contacts, audit = build()
    first = await manager.handle(inbound_work("borrar mis datos"))
    assert first.replies[0].content.text == texts.ERASE_CONFIRMATION_REQUEST
    assert contacts.erasures == []
    confirmed = await manager.handle(inbound_work("BORRAR CONFIRMAR"))
    assert contacts.erasures == [inbound_work().contact.id]
    assert confirmed.state is ConversationState.CLOSED
    assert "contact_erasure_requested" in audit.actions


async def test_throttled_contact_gets_a_single_notice() -> None:
    manager, _, _ = build(throttle=FakeThrottle(allowed=False, notify=True))
    noticed = await manager.handle(inbound_work("hola"))
    assert noticed.outcome == "throttled"
    assert noticed.replies[0].content.text == texts.TOO_MANY_MESSAGES
    manager, _, _ = build(throttle=FakeThrottle(allowed=False, notify=False))
    silent = await manager.handle(inbound_work("hola"))
    assert silent.status is InboundStatus.IGNORED and silent.replies == ()


async def test_non_text_content_gets_a_notice_and_reactions_are_silent() -> None:
    manager, _, _ = build()
    image = await manager.handle(inbound_work(None, kind=MessageKind.IMAGE))
    assert image.replies[0].content.text == texts.UNSUPPORTED_CONTENT
    reaction = await manager.handle(inbound_work(None, kind=MessageKind.REACTION))
    assert reaction.status is InboundStatus.IGNORED


async def test_handoff_is_suggested_after_repeated_no_info() -> None:
    engine = FakeEngine(answer_to_give=Answer("No tengo esa información.", Outcome.NO_INFO))
    manager, _, _ = build(engine, config=ManagerConfig(handoff_after_no_info=2))
    first = await manager.handle(inbound_work("¿Qué pasa con el trámite raro?"))
    assert first.no_info_count == 1 and len(first.replies) == 1
    second_work = replace(inbound_work("¿Y el otro trámite raro?"), conversation=conversation(no_info_count=1))
    second = await manager.handle(second_work)
    assert second.no_info_count == 2
    assert second.replies[-1].content.text == texts.HANDOFF_SUGGESTION


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (InvalidRequest("La consulta está vacía."), "invalid_request"),
        (QuotaExceeded("Límite diario.", 60), "quota_exceeded"),
    ],
)
async def test_user_errors_become_replies(error: Exception, outcome: str) -> None:
    manager, _, _ = build(FakeEngine(error=error))
    decision = await manager.handle(inbound_work("pregunta"))
    assert decision.outcome == outcome
    assert decision.replies[0].content.text == error.user_message  # type: ignore[attr-defined]


async def test_transient_errors_propagate_for_retry() -> None:
    manager, _, _ = build(FakeEngine(error=ServiceBusy("ocupado")))
    with pytest.raises(ServiceBusy):
        await manager.handle(inbound_work("pregunta"))


async def test_link_flow_creates_and_redeems_code() -> None:
    manager, contacts, audit = build()
    created = await manager.handle(inbound_work("VINCULAR"))
    assert len(contacts.codes) == 1
    code = next(iter(contacts.codes)).removeprefix("h:")
    assert f"{code[:3]}-{code[3:]}" in created.replies[0].content.text
    redeemed = await manager.handle(inbound_work(f"vincular {code[:3]}-{code[3:]}"))
    assert redeemed.replies[0].content.text == texts.LINK_DONE
    assert "contacts_linked" in audit.actions


async def test_link_invalid_and_disabled() -> None:
    manager, contacts, _ = build()
    contacts.redeem_ok = False
    invalid = await manager.handle(inbound_work("VINCULAR ABC-234"))
    assert invalid.replies[0].content.text == texts.LINK_INVALID
    manager, _, _ = build(config=ManagerConfig(identity_linking_enabled=False))
    disabled = await manager.handle(inbound_work("VINCULAR"))
    assert disabled.replies[0].content.text == texts.LINK_DISABLED


async def test_privacy_notice_includes_retention() -> None:
    manager, _, _ = build(config=ManagerConfig(retention_messages_days=45))
    decision = await manager.handle(inbound_work("privacidad"))
    assert "45" in decision.replies[0].content.text
