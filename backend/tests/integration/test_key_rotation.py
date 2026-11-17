"""Rotación de la clave de cifrado contra PostgreSQL: el mantenimiento recifra lo guardado con la
clave anterior, alcanza lo que se escribe con ella durante el despliegue y nunca pisa un dato más
nuevo. Al final, la clave anterior se puede retirar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import asyncpg
import pytest

from iamia.channels.models import Channel, WebhookBatch
from iamia.core.clock import utcnow
from iamia.integrations.x.tokens import StoredToken
from iamia.persistence.codec import DataCodec
from iamia.persistence.inbox import InboxRepository
from iamia.persistence.oauth import PgTokenStore
from iamia.persistence.rotation import KeyRotationRepository, key_usage
from iamia.security.crypto import FieldCipher, Pseudonymizer, generate_key
from iamia.workers.maintenance import MaintenanceConfig, MaintenanceRunner
from tests.factories import inbound_message
from tests.integration.conftest import Stack

OLD_KEY, NEW_KEY, PSEUDONYM_KEY = generate_key(), generate_key(), generate_key()
ROTATED = f"k2:{NEW_KEY},k1:{OLD_KEY}"
NEW_ONLY = f"k2:{NEW_KEY}"


def _codec(keys: str) -> DataCodec:
    return DataCodec(FieldCipher.from_config(keys), Pseudonymizer.from_config(PSEUDONYM_KEY))


@pytest.fixture
def codec() -> DataCodec:
    """El stack escribe con la clave anterior, como los procesos antes de la rotación."""
    return _codec(f"k1:{OLD_KEY}")


def _token(suffix: str) -> StoredToken:
    return StoredToken(f"acceso-{suffix}", f"renovacion-{suffix}", utcnow() + timedelta(hours=2))


async def _write(stack: Stack, provider_id: str, sender: str) -> None:
    message = inbound_message(provider_id, sender_id=sender, text=f"consulta {provider_id}")
    await stack.ingest.ingest(WebhookBatch(messages=(message,)))
    await stack.drain()


async def _populate(stack: Stack) -> None:
    for provider_id, sender in (("r-1", "5551111"), ("r-2", "5552222"), ("r-3", "5551111")):
        await _write(stack, provider_id, sender)
    await PgTokenStore(stack.pool, stack.codec).save("x", _token("1"))


async def _plaintexts(stack: Stack, codec: DataCodec) -> dict[str, str]:
    """Todo lo cifrado, leído con `codec` (falla si algún dato necesita una clave que no tiene)."""
    values: dict[str, str] = {}
    for row in await stack.pool.fetch("SELECT id, channel, address_hash, address_enc FROM contacts"):
        values[f"contact:{row['id']}"] = codec.decrypt_address(row["channel"], row["address_hash"], row["address_enc"])
    for row in await stack.pool.fetch("SELECT id, body_enc FROM messages WHERE body_enc IS NOT NULL"):
        values[f"message:{row['id']}"] = codec.decrypt_text(str(row["id"]), row["body_enc"]) or ""
    token = await PgTokenStore(stack.pool, codec).load("x")
    assert token is not None
    values["x"] = f"{token.access_token}|{token.refresh_token}"
    return values


def _runner(stack: Stack, rotation: KeyRotationRepository) -> MaintenanceRunner:
    return MaintenanceRunner(
        repository=stack.maintenance_repo,
        work=stack.work,
        publisher=stack.publisher,
        audit=stack.audit,
        semantic_cache=None,
        rotation=rotation,
        config=MaintenanceConfig(rotation_budget_seconds=30),
    )


async def test_everything_is_reencrypted_and_the_old_key_can_be_retired(stack: Stack) -> None:
    await _populate(stack)
    before = await _plaintexts(stack, stack.codec)
    assert len(before) == 2 + 6 + 1  # 2 contactos, 3 consultas con sus 3 respuestas y los tokens de X
    assert await key_usage(stack.pool) == {"oauth_tokens": {"k1": 2}, "contacts": {"k1": 2}, "messages": {"k1": 6}}

    # Lotes de 2 para recorrer varias páginas. Sin margen: el despliegue ya terminó.
    rotation = KeyRotationRepository(stack.pool, _codec(ROTATED), batch_size=2, grace_seconds=0)
    runner = _runner(stack, rotation)
    assert await runner.rotate_keys() == {"rotated": 1 + 2 + 6, "failed": 0}
    assert runner.rotation_done

    assert await key_usage(stack.pool) == {"oauth_tokens": {"k2": 2}, "contacts": {"k2": 2}, "messages": {"k2": 6}}
    # Solo con la clave nueva se lee todo igual que antes.
    assert await _plaintexts(stack, _codec(NEW_ONLY)) == before
    rows = await stack.pool.fetch("SELECT target, key_id, finished_at FROM key_rotation")
    assert sorted((row["target"], row["key_id"]) for row in rows) == [
        ("contacts", "k2"),
        ("messages", "k2"),
        ("oauth_tokens", "k2"),
    ]
    assert all(row["finished_at"] is not None for row in rows)

    # Un proceso nuevo con la misma configuración no vuelve a recorrer nada.
    assert await KeyRotationRepository(stack.pool, _codec(ROTATED)).pending() == []


async def test_data_written_with_the_old_key_during_the_deploy_is_caught_after_the_grace_period(
    stack: Stack,
) -> None:
    await _populate(stack)
    runner = _runner(stack, KeyRotationRepository(stack.pool, _codec(ROTATED), grace_seconds=3600))
    assert await runner.rotate_keys() == {"rotated": 9, "failed": 0}
    assert not runner.rotation_done  # recifró todo, pero falta la vuelta después del margen

    # Un proceso que todavía tiene la configuración anterior escribe con la clave vieja.
    await _write(stack, "tarde", "5553333")
    assert await runner.rotate_keys() == {"rotated": 0, "failed": 0}  # dentro del margen, espera
    assert not runner.rotation_done

    # Vence el margen (se atrasa el reloj de la rotación dos horas).
    await stack.pool.execute(
        "UPDATE key_rotation SET started_at = started_at - interval '2 hours', "
        "pass_started_at = pass_started_at - interval '2 hours'"
    )
    totals = await runner.rotate_keys()
    assert totals == {"rotated": 3, "failed": 0}  # el contacto nuevo, su consulta y la respuesta
    assert runner.rotation_done
    usage = await key_usage(stack.pool)
    assert all(set(counts) == {"k2"} for counts in usage.values())
    await _plaintexts(stack, _codec(NEW_ONLY))


async def test_with_a_single_key_there_is_no_grace_period_to_wait(stack: Stack) -> None:
    """Instalación nueva: todo está con la única clave y queda al día en el primer ciclo."""
    await _populate(stack)
    rotation = KeyRotationRepository(stack.pool, stack.codec, grace_seconds=3600)
    runner = _runner(stack, rotation)
    assert await runner.rotate_keys() == {"rotated": 0, "failed": 0}
    assert runner.rotation_done
    assert await rotation.pending() == []


class _ChangesBeforeWriting:
    """Pool que cambia el dato justo antes de que la rotación escriba el suyo."""

    def __init__(self, pool: asyncpg.Pool, change: Callable[[], Awaitable[None]]) -> None:
        self._pool = pool
        self._change: Callable[[], Awaitable[None]] | None = change

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def execute(self, query: str, *args: Any) -> str:
        if "unnest" in query and self._change is not None:
            change, self._change = self._change, None
            await change()
        return await self._pool.execute(query, *args)


async def test_a_value_that_changed_meanwhile_is_never_overwritten(stack: Stack) -> None:
    await PgTokenStore(stack.pool, stack.codec).save("x", _token("1"))
    rotated = _codec(ROTATED)

    async def renew() -> None:  # el dispatcher renueva el token de X en ese mismo momento
        await PgTokenStore(stack.pool, rotated).save("x", _token("2"))

    rotation = KeyRotationRepository(_ChangesBeforeWriting(stack.pool, renew), rotated, grace_seconds=0)  # type: ignore[arg-type]
    step = await rotation.step("oauth_tokens")
    assert step.rotated == 0  # el token viejo recifrado no reemplazó al renovado
    token = await PgTokenStore(stack.pool, _codec(NEW_ONLY)).load("x")
    assert token is not None and (token.access_token, token.refresh_token) == ("acceso-2", "renovacion-2")


async def test_data_that_cannot_be_decrypted_is_counted_and_heals_when_the_person_writes_again(
    stack: Stack,
) -> None:
    await _populate(stack)
    # La dirección de un contacto quedó cifrada con una clave que ya no está en el anillo.
    address_hash = stack.codec.address_hash(Channel.TELEGRAM, "5552222")
    lost = _codec(f"k0:{generate_key()}").encrypt_address(Channel.TELEGRAM, address_hash, "5552222")
    await stack.pool.execute("UPDATE contacts SET address_enc = $2 WHERE address_hash = $1", address_hash, lost)

    rotated = _codec(ROTATED)
    runner = _runner(stack, KeyRotationRepository(stack.pool, rotated, grace_seconds=0))
    totals = await runner.rotate_keys()
    assert totals["failed"] >= 1 and runner.rotation_done  # se cuenta y no frena el resto
    assert await key_usage(stack.pool) == {
        "oauth_tokens": {"k2": 2},
        "contacts": {"k0": 1, "k2": 1},
        "messages": {"k2": 6},
    }
    assert await stack.pool.fetchval("SELECT failed FROM key_rotation WHERE target = 'contacts'") == 1

    # Cuando esa persona vuelve a escribir, su dirección se cifra de nuevo con la clave activa.
    await InboxRepository(stack.pool, rotated, idle_minutes=30).store(inbound_message("r-4", sender_id="5552222"))
    row = await stack.pool.fetchrow("SELECT channel, address_enc FROM contacts WHERE address_hash = $1", address_hash)
    assert row is not None
    assert _codec(NEW_ONLY).decrypt_address(row["channel"], address_hash, row["address_enc"]) == "5552222"
