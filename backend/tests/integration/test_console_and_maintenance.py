"""Derivación a operadores, consola, mantenimiento, vinculación de identidades y chat web."""

from __future__ import annotations

import re

import pytest

from iamia.channels.models import Channel, IdentityChange, WebhookBatch
from iamia.conversations import texts
from iamia.core.clock import utcnow
from iamia.core.errors import Conflict, Forbidden
from iamia.persistence.maintenance import RetentionPolicy
from iamia.security.rbac import Principal, Role
from tests.factories import inbound_message
from tests.integration.conftest import Stack

ANA = Principal("ana", frozenset({Role.OPERATOR}), "Ana")
BETO = Principal("beto", frozenset({Role.OPERATOR}), "Beto")
AUDITOR = Principal("aud", frozenset({Role.AUDITOR}))
ADMIN = Principal("root", frozenset({Role.ADMIN}))


async def _handoff(stack: Stack) -> str:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("h-1", text="quiero hablar con una persona"),)))
    await stack.drain()
    return str(await stack.pool.fetchval("SELECT id FROM conversations"))


async def test_handoff_claim_reply_release(stack: Stack) -> None:
    conversation_id = await _handoff(stack)
    assert stack.sender.sent[-1][1] == texts.HANDOFF_STARTED
    waiting = await stack.console.list(ANA, ["waiting_human"])
    assert [item["id"] for item in waiting] == [conversation_id]
    assert "5551234" not in waiting[0]["contact"]  # la consola ve la dirección enmascarada

    # Mientras espera a una persona el bot no responde.
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("h-2", text="¿hola?"),)))
    await stack.drain()
    assert len(stack.sender.sent) == 1

    await stack.console.claim(ANA, conversation_id)
    with pytest.raises(Conflict):
        await stack.console.claim(BETO, conversation_id)  # ya la tiene otra persona
    with pytest.raises(Conflict):
        await stack.console.reply(BETO, conversation_id, "Hola, soy Beto")

    await stack.console.reply(ANA, conversation_id, "Hola, soy Ana. ¿En qué te ayudo?")
    await stack.drain()
    _recipient, text, context = stack.sender.sent[-1]
    assert text == "Hola, soy Ana. ¿En qué te ayudo?" and context.operator_reply

    detail = await stack.console.detail(ANA, conversation_id)
    authors = [m["author"] for m in detail["messages"]]
    assert authors[:3] == ["user", "bot", "user"] and authors[-1] == "operator"
    assert detail["state"] == "human" and detail["assigned_to"] == "ana"

    await stack.console.release(ANA, conversation_id)
    await stack.drain()
    assert stack.sender.sent[-1][1] == texts.HANDOFF_RELEASED
    assert await stack.pool.fetchval("SELECT state FROM conversations") == "bot"

    actions = [row["action"] for row in await stack.audit.list(limit=50)]
    assert {"conversation_claimed", "conversation_replied", "conversation_released"} <= set(actions)

    # La consola refresca la conversación abierta: se audita la primera vista, no cada refresco.
    for _ in range(3):
        await stack.console.detail(ANA, conversation_id)
    actions = [row["action"] for row in await stack.audit.list(limit=50)]
    assert actions.count("conversation_viewed") == 1


async def test_the_queue_shows_the_longest_wait_first_and_the_other_lists_the_newest_first(stack: Stack) -> None:
    for index in range(3):
        message = inbound_message(f"o-{index}", sender_id=f"555000{index}")
        await stack.ingest.ingest(WebhookBatch(messages=(message,)))
    await stack.drain()
    ids = [str(row["id"]) for row in await stack.pool.fetch("SELECT id FROM conversations ORDER BY opened_at")]
    for minutes, conversation_id in zip((30, 20, 10), ids, strict=True):  # la primera, la más vieja
        await stack.pool.execute(
            "UPDATE conversations SET last_activity_at = now() - make_interval(mins => $2) WHERE id::text = $1",
            conversation_id,
            minutes,
        )
    # Con miles de conversaciones con el asistente, lo útil es lo más reciente.
    assert [item["id"] for item in await stack.console.list(ANA, ["bot"])] == ids[::-1]
    await stack.pool.execute("UPDATE conversations SET state = 'waiting_human'")
    # En la cola de atención, primero la que más espera.
    assert [item["id"] for item in await stack.console.list(ANA, ["waiting_human"])] == ids


async def test_permissions_are_enforced(stack: Stack) -> None:
    conversation_id = await _handoff(stack)
    with pytest.raises(Forbidden):
        await stack.console.list(AUDITOR, ["waiting_human"])
    with pytest.raises(Forbidden):
        await stack.console.erase_contact(ANA, conversation_id)
    await stack.console.erase_contact(ADMIN, conversation_id)
    assert await stack.pool.fetchval("SELECT count(*) FROM contacts") == 0
    assert await stack.pool.fetchval("SELECT count(*) FROM messages") == 0


async def test_close_conversation(stack: Stack) -> None:
    conversation_id = await _handoff(stack)
    await stack.console.claim(ANA, conversation_id)
    await stack.console.close(ANA, conversation_id)
    assert await stack.pool.fetchval("SELECT state FROM conversations") == "closed"
    # Un mensaje nuevo abre otra conversación con el bot.
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("h-9", text="¿Cómo saco el DNI?"),)))
    await stack.drain()
    assert await stack.pool.fetchval("SELECT count(*) FROM conversations") == 2
    assert stack.engine.questions[-1] == "¿Cómo saco el DNI?"


async def test_expired_handoff_returns_to_bot_with_notice(stack: Stack) -> None:
    await _handoff(stack)
    await stack.pool.execute("UPDATE conversations SET handoff_at = now() - interval '5 hours'")
    await stack.maintenance.sweep()
    await stack.drain()
    assert stack.sender.sent[-1][1] == texts.HANDOFF_TIMEOUT
    assert await stack.pool.fetchval("SELECT state FROM conversations") == "bot"


async def test_erasure_request_is_applied_by_housekeeping(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("e-1", text="BORRAR CONFIRMAR"),)))
    await stack.drain()
    assert stack.sender.sent[-1][1] == texts.ERASE_DONE  # primero se confirma por el mismo canal
    await stack.pool.execute("UPDATE contacts SET erase_requested_at = now() - interval '10 minutes'")
    result = await stack.maintenance.housekeeping()
    assert result["erased_contacts"] == 1
    assert await stack.pool.fetchval("SELECT count(*) FROM messages") == 0


async def test_retention_deletes_old_data(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("t-1"),)))
    await stack.drain()
    await stack.pool.execute("UPDATE messages SET created_at = now() - interval '200 days'")
    await stack.pool.execute("UPDATE conversations SET state = 'closed', closed_at = now() - interval '200 days'")
    await stack.pool.execute("UPDATE contacts SET last_seen_at = now() - interval '400 days'")
    deleted = await stack.maintenance_repo.apply_retention(RetentionPolicy(90, 180, 365))
    assert deleted["messages"] == 2
    assert await stack.pool.fetchval("SELECT count(*) FROM contacts") == 0


async def test_identity_linking_between_channels(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("k-1", text="VINCULAR"),)))
    await stack.drain()
    found = re.search(r"[A-Z0-9]{3}-[A-Z0-9]{3}", stack.sender.sent[-1][1])
    assert found, stack.sender.sent[-1][1]
    code = found.group(0)
    other = inbound_message("k-2", text=f"VINCULAR {code}", sender_id="9998887")
    await stack.ingest.ingest(WebhookBatch(messages=(other,)))
    await stack.drain()
    assert stack.sender.sent[-1][1] == texts.LINK_DONE
    persons = await stack.pool.fetchval("SELECT count(DISTINCT person_id) FROM contacts WHERE person_id IS NOT NULL")
    assert persons == 1 and await stack.pool.fetchval("SELECT count(*) FROM contacts") == 2
    # El código es de un solo uso.
    third = inbound_message("k-3", text=f"VINCULAR {code}", sender_id="7776665")
    await stack.ingest.ingest(WebhookBatch(messages=(third,)))
    await stack.drain()
    assert stack.sender.sent[-1][1] == texts.LINK_INVALID


async def test_identity_change_keeps_the_conversation(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("i-1", sender_id="old-id"),)))
    change = IdentityChange(Channel.TELEGRAM, "old-id", "new-id", utcnow())
    result = await stack.ingest.ingest(WebhookBatch(identity_changes=(change,)))
    assert result.identity_changes == 1
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("i-2", sender_id="new-id"),)))
    assert await stack.pool.fetchval("SELECT count(*) FROM contacts") == 1
    assert await stack.pool.fetchval("SELECT count(*) FROM conversations") == 1


async def test_webchat_messages_after(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("w-1", channel=Channel.TELEGRAM),)))
    await stack.drain()
    session_rows = await stack.webchat_repo.messages_after("5551234", 0)
    # Solo el chat web usa este repositorio; para otro canal no hay mensajes de la sesión.
    assert session_rows == []


async def test_webchat_replies_are_visible_before_dispatch(stack: Stack) -> None:
    """En el chat web el sondeo es la entrega: la respuesta se ve apenas queda guardada."""
    message = inbound_message("w-2", channel=Channel.WEBCHAT, sender_id="sesion-1", account_id="webchat")
    await stack.ingest.ingest(WebhookBatch(messages=(message,)))
    inbound_jobs = [job for job, _delay in stack.queue.published if job.direction == "inbound"]
    stack.queue.published.clear()
    for job in inbound_jobs:
        await stack.inbound.handle(job)  # solo el worker: el dispatcher todavía no corrió
    rows = await stack.webchat_repo.messages_after("sesion-1", 0)
    assert [row["author"] for row in rows] == ["bot"]
    assert await stack.pool.fetchval("SELECT status FROM messages WHERE direction = 'out'") == "pending"
    assert await stack.webchat_repo.messages_after("sesion-1", rows[0]["seq"]) == []


async def test_channel_summary(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("c-1"),)))
    await stack.drain()
    summary = await stack.console.summary(ADMIN)
    assert {(row["direction"], row["status"]) for row in summary} == {("in", "answered"), ("out", "sent")}
