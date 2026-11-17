"""Camino completo de un mensaje con PostgreSQL y Redis reales (modelo y canal simulados)."""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest

from iamia.channels.models import Channel, DeliveryError, DeliveryState, FailureKind, StatusUpdate, WebhookBatch
from iamia.conversations import texts
from iamia.core.clock import utcnow
from iamia.core.errors import ServiceUnavailable
from iamia.messaging.contracts import Job
from iamia.persistence.work import WorkRepository
from tests.factories import inbound_message
from tests.integration.conftest import Stack


async def _status(stack: Stack, provider_id: str) -> tuple[str, str]:
    row = await stack.pool.fetchrow(
        "SELECT direction, status FROM messages WHERE provider_message_id = $1", provider_id
    )
    assert row is not None
    return row["direction"], row["status"]


async def test_question_is_answered_and_delivered(stack: Stack) -> None:
    result = await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("m-1", text="¿Cómo saco el DNI?"),)))
    assert result.accepted == 1
    await stack.drain()

    assert stack.engine.questions == ["¿Cómo saco el DNI?"]
    assert len(stack.sender.sent) == 1
    recipient, text, _ = stack.sender.sent[0]
    assert recipient.address == "5551234"  # la dirección se descifró para enviar
    assert "DNI digital" in text and "https://www.argentina.gob.ar/dni" in text
    assert await _status(stack, "m-1") == ("in", "answered")
    out = await stack.pool.fetchrow(
        "SELECT status, provider_message_id, enqueued_at FROM messages WHERE direction='out'"
    )
    assert out["status"] == "sent" and out["provider_message_id"] == "prov-1" and out["enqueued_at"] is not None


async def test_texts_and_addresses_are_encrypted_at_rest(stack: Stack) -> None:
    message = inbound_message("m-pii", text="Mi DNI es 30.123.456 y mi mail ana@example.com")
    await stack.ingest.ingest(WebhookBatch(messages=(message,)))
    row = await stack.pool.fetchrow(
        "SELECT m.body_enc, c.address_enc, c.address_hash FROM messages m JOIN contacts c ON c.id = m.contact_id"
    )
    assert "30.123.456" not in row["body_enc"] and "ana@example.com" not in row["body_enc"]
    assert "5551234" not in row["address_enc"] and "5551234" not in row["address_hash"]
    work = await stack.work.claim_inbound(str(await stack.pool.fetchval("SELECT id FROM messages")), lease_seconds=60)
    assert work is not None
    assert "[DNI]" in (work.text or "") and "[EMAIL]" in (work.text or "")  # se guarda enmascarado


async def test_duplicates_are_ignored_by_redis_and_by_postgres(stack: Stack, real_redis) -> None:
    batch = WebhookBatch(messages=(inbound_message("dup-1"),))
    first = await stack.ingest.ingest(batch)
    again = await stack.ingest.ingest(batch)
    assert (first.accepted, again.duplicated) == (1, 1)
    await real_redis.flushdb()  # sin la marca de Redis decide la restricción única de PostgreSQL
    third = await stack.ingest.ingest(batch)
    assert third.duplicated == 1
    assert await stack.pool.fetchval("SELECT count(*) FROM messages") == 1


async def test_stale_messages_are_not_answered(stack: Stack) -> None:
    old = inbound_message("old-1", sent_at=utcnow() - timedelta(days=3))
    result = await stack.ingest.ingest(WebhookBatch(messages=(old,)))
    assert result.stale == 1 and result.accepted == 0


async def test_conversation_and_contact_are_reused(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("a-1"),)))
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("a-2"),)))
    assert await stack.pool.fetchval("SELECT count(*) FROM contacts") == 1
    assert await stack.pool.fetchval("SELECT count(*) FROM conversations") == 1


async def test_replies_follow_the_thread_and_subject_of_the_last_message(stack: Stack) -> None:
    """Dos correos con distinto asunto en la misma conversación: cada respuesta va a su hilo."""
    for provider_id, subject in (("c-1", "Consulta DNI"), ("c-2", "Otra consulta")):
        message = inbound_message(provider_id, subject=subject, thread_ref=f"<{provider_id}@correo.test>")
        await stack.ingest.ingest(WebhookBatch(messages=(message,)))
        await stack.drain()
    first, second = (recipient for recipient, _, _ in stack.sender.sent)
    assert (first.subject, first.thread_ref) == ("Consulta DNI", "<c-1@correo.test>")
    assert (second.subject, second.thread_ref) == ("Otra consulta", "<c-2@correo.test>")
    # Un mensaje sin asunto (otro canal o un correo sin Subject) no borra el que había.
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("c-3"),)))
    await stack.drain()
    third = stack.sender.sent[-1][0]
    assert (third.subject, third.thread_ref) == ("Otra consulta", "<c-2@correo.test>")


async def test_queue_down_leaves_message_for_the_sweeper(stack: Stack) -> None:
    stack.queue.fail_publish = True
    result = await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("q-1"),)))
    assert result.accepted == 1  # la plataforma recibe 200 igual
    assert await stack.pool.fetchval("SELECT enqueued_at FROM messages") is None
    stack.queue.fail_publish = False
    await stack.pool.execute("UPDATE messages SET created_at = now() - interval '2 minutes'")
    assert await stack.maintenance.sweep() == 1
    await stack.drain()
    assert len(stack.sender.sent) == 1


async def test_transient_engine_error_is_retried_then_answered(stack: Stack) -> None:
    stack.engine.error = ServiceUnavailable("modelo caído")
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("r-1"),)))
    job, _ = stack.queue.published.pop()
    await stack.inbound.handle(job)
    assert await _status(stack, "r-1") == ("in", "received")  # liberado para reintentar
    retry, delay = stack.queue.published.pop()
    assert retry.retries == 1 and delay >= 1
    stack.engine.error = None
    await stack.inbound.handle(retry)
    await stack.drain()
    assert await _status(stack, "r-1") == ("in", "answered")


async def test_unexpected_errors_release_the_message_for_the_queue_retry(stack: Stack) -> None:
    # La base corta la conexión a mitad del proceso: el mensaje no queda reservado hasta que venza
    # la reserva; se libera y el reintento con espera de la cola lo responde enseguida.
    stack.engine.error = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("x-1"),)))
    job, _ = stack.queue.published.pop()
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await stack.inbound.handle(job)
    assert await _status(stack, "x-1") == ("in", "received")
    stack.engine.error = None
    await stack.inbound.handle(job.retried())
    await stack.drain()
    assert await _status(stack, "x-1") == ("in", "answered")


async def test_a_failed_claim_does_not_leave_the_message_reserved(stack: Stack) -> None:
    # La reserva y las lecturas van en una transacción: si algo falla después del UPDATE (acá, un
    # LIMIT negativo en la lectura del historial), el mensaje no queda reservado hasta que venza.
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("c-1"),)))
    message_id = str(await stack.pool.fetchval("SELECT id FROM messages"))
    broken = WorkRepository(stack.pool, stack.codec, context_turns=-1)
    with pytest.raises(asyncpg.PostgresError):
        await broken.claim_inbound(message_id, 60)
    assert await _status(stack, "c-1") == ("in", "received")
    assert await stack.work.claim_inbound(message_id, 60) is not None


async def test_engine_down_until_max_attempts_sends_unavailable_notice(stack: Stack) -> None:
    stack.engine.error = ServiceUnavailable("modelo caído")
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("u-1"),)))
    for _ in range(10):
        pending = list(stack.queue.published)
        stack.queue.published.clear()
        if not pending:
            break
        for job, _ in pending:
            await (stack.inbound.handle if job.direction == "inbound" else stack.outbound.handle)(job)
    assert await _status(stack, "u-1") == ("in", "answered")
    assert "no está disponible" in stack.sender.sent[-1][1]


async def test_lease_prevents_double_processing_and_expires(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("l-1"),)))
    message_id = str(await stack.pool.fetchval("SELECT id FROM messages"))
    assert await stack.work.claim_inbound(message_id, 60) is not None
    assert await stack.work.claim_inbound(message_id, 60) is None  # otro worker no lo toma
    await stack.pool.execute("UPDATE messages SET lease_until = now() - interval '1 minute'")
    assert await stack.work.claim_inbound(message_id, 60) is not None  # el worker anterior murió


async def test_delivery_statuses_only_move_forward(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("s-1"),)))
    await stack.drain()
    now = utcnow()
    read = StatusUpdate(Channel.TELEGRAM, "prov-1", DeliveryState.READ, now)
    delivered = StatusUpdate(Channel.TELEGRAM, "prov-1", DeliveryState.DELIVERED, now)
    await stack.ingest.ingest(WebhookBatch(statuses=(read,)))
    await stack.ingest.ingest(WebhookBatch(statuses=(delivered,)))  # llega tarde: no retrocede
    assert await stack.pool.fetchval("SELECT status FROM messages WHERE direction = 'out'") == "read"


async def test_retryable_delivery_error_then_success(stack: Stack) -> None:
    stack.sender.failures.append(DeliveryError(FailureKind.RETRYABLE, "502", code="502"))
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("d-1"),)))
    await stack.drain()
    assert len(stack.sender.sent) == 1
    row = await stack.pool.fetchrow("SELECT status, attempts FROM messages WHERE direction = 'out'")
    assert row["status"] == "sent" and row["attempts"] == 2


async def test_permanent_delivery_error_marks_failed_and_shows_in_problems(stack: Stack) -> None:
    from iamia.security.rbac import Principal, Role

    stack.sender.failures.append(DeliveryError(FailureKind.PERMANENT, "usuario bloqueó el bot", code="403"))
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("p-1"),)))
    await stack.drain()
    assert await stack.pool.fetchval("SELECT status FROM messages WHERE direction = 'out'") == "failed"
    admin = Principal("admin", frozenset({Role.ADMIN}))
    problems = await stack.console.problems(admin)
    assert problems and problems[0]["error_code"] == "permanent:403"
    await stack.console.retry(admin, problems[0]["id"])
    await stack.drain()
    assert await stack.pool.fetchval("SELECT status FROM messages WHERE direction = 'out'") == "sent"


async def test_auth_error_opens_the_channel_circuit(stack: Stack) -> None:
    stack.sender.failures.append(DeliveryError(FailureKind.AUTH, "token vencido", code="190"))
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("x-1"),)))
    job, _ = stack.queue.published.pop()
    await stack.inbound.handle(job)
    out_job, _ = stack.queue.published.pop()
    await stack.outbound.handle(out_job)
    assert not stack.breaker.allow()
    retry, delay = stack.queue.published.pop()
    assert retry.message_id == out_job.message_id and delay >= 1


async def test_exhausted_delivery_goes_to_dead_letter(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("z-1"),)))
    job, _ = stack.queue.published.pop()
    await stack.inbound.handle(job)
    out_job, _ = stack.queue.published.pop()
    await stack.pool.execute("UPDATE messages SET attempts = 7 WHERE direction = 'out'")
    stack.sender.failures.append(DeliveryError(FailureKind.RETRYABLE, "timeout", code="timeout"))
    await stack.outbound.handle(out_job)
    assert await stack.pool.fetchval("SELECT status FROM messages WHERE direction = 'out'") == "dead"
    assert stack.queue.dead and stack.queue.dead[0][0].message_id == out_job.message_id


async def test_unknown_job_ids_are_ignored(stack: Stack) -> None:
    await stack.inbound.handle(Job("inbound", "00000000-0000-4000-8000-000000000000", "telegram"))
    await stack.outbound.handle(Job("outbound", "00000000-0000-4000-8000-000000000000", "telegram"))
    assert stack.sender.sent == []


async def test_opt_out_and_resume(stack: Stack) -> None:
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("o-1", text="BAJA"),)))
    await stack.drain()
    assert await stack.pool.fetchval("SELECT opted_out FROM contacts") is True
    assert stack.sender.sent[-1][1] == texts.OPT_OUT_DONE
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("o-2", text="¿Cómo saco el DNI?"),)))
    await stack.drain()
    assert len(stack.sender.sent) == 1  # dada de baja: no se responde
    await stack.ingest.ingest(WebhookBatch(messages=(inbound_message("o-3", text="ALTA"),)))
    await stack.drain()
    assert await stack.pool.fetchval("SELECT opted_out FROM contacts") is False
    assert stack.sender.sent[-1][1] == texts.OPT_IN_DONE
