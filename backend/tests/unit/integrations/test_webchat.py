"""Chat web: aviso por Redis Pub/Sub y long polling (iamia/integrations/webchat/adapter.py)."""

from __future__ import annotations

import asyncio
import logging

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from iamia.channels.models import (
    Channel,
    DeliveryError,
    FailureKind,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
)
from iamia.integrations.webchat.adapter import (
    WebchatHub,
    WebchatPublisher,
    WebchatSender,
    build_webchat_adapter,
    notification_channel,
)

from .support import NOW

RECIPIENT = Recipient(Channel.WEBCHAT, "sesion-1", "webchat")


async def wait_subscribed(redis: fakeredis.FakeAsyncRedis) -> None:
    """Espera a que el hub se suscriba al patrón (lo hace en una tarea de fondo)."""
    for _ in range(200):
        if await redis.pubsub_numpat() >= 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("el hub no se suscribió a Redis")


@pytest.fixture
async def hub(bytes_redis):
    hub = WebchatHub(bytes_redis)
    await hub.start()
    await wait_subscribed(bytes_redis)
    yield hub
    await hub.stop()


def test_canal_de_notificacion():
    assert notification_channel("sesion-1") == "iamia:webchat:sesion-1"


async def test_send_publica_el_aviso_en_el_canal_de_la_sesion(bytes_redis):
    pubsub = bytes_redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe("iamia:webchat:sesion-1")
    sender = WebchatSender(WebchatPublisher(bytes_redis))
    result = await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW, message_id="msg-1"))
    assert result == SendResult("msg-1")  # la respuesta ya está guardada: el id es el interno
    received = []
    for _ in range(50):  # get_message devuelve None mientras descarta la confirmación de la suscripción
        message = await pubsub.get_message(timeout=0.05)
        if message is not None:
            received.append((message["channel"], message["data"]))
            break
    await pubsub.aclose()
    assert received == [(b"iamia:webchat:sesion-1", b"1")]


async def test_send_sin_id_interno(bytes_redis):
    sender = WebchatSender(WebchatPublisher(bytes_redis))
    assert await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW)) == SendResult(None)


class BrokenRedis:
    async def publish(self, channel: str, message: str) -> int:
        raise RedisConnectionError("Redis no responde")


async def test_send_falla_reintentable_si_redis_no_responde():
    sender = WebchatSender(WebchatPublisher(BrokenRedis()))
    with pytest.raises(DeliveryError) as caught:
        await sender.send(RECIPIENT, RenderedPart("Hola"), SendContext(now=NOW))
    assert (caught.value.kind, caught.value.code) == (FailureKind.RETRYABLE, "redis")


async def test_wait_despierta_solo_a_la_sesion_avisada(hub, bytes_redis):
    publisher = WebchatPublisher(bytes_redis)
    first = asyncio.create_task(hub.wait("sesion-1", max_wait=5))
    second = asyncio.create_task(hub.wait("sesion-1", max_wait=5))
    other = asyncio.create_task(hub.wait("sesion-2", max_wait=0.3))
    await asyncio.sleep(0)
    assert hub.waiting == 3
    await publisher.publish("sesion-1")
    assert await asyncio.wait_for(asyncio.gather(first, second), timeout=3) == [True, True]
    assert await other is False
    assert hub.waiting == 0  # no quedan esperas colgadas


async def test_wait_vence_y_limpia_la_espera(bytes_redis):
    hub = WebchatHub(bytes_redis)  # sin iniciar: nadie la despierta
    assert await hub.wait("sesion-1", max_wait=0.05) is False
    assert hub.waiting == 0


async def test_hub_con_respuestas_decodificadas():
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    hub = WebchatHub(redis)
    await hub.start()
    try:
        await wait_subscribed(redis)
        waiter = asyncio.create_task(hub.wait("sesion-9", max_wait=5))
        await asyncio.sleep(0)
        await WebchatPublisher(redis).publish("sesion-9")
        assert await asyncio.wait_for(waiter, timeout=3) is True
    finally:
        await hub.stop()
        await redis.aclose()


async def test_start_es_idempotente_y_stop_cancela_la_escucha(bytes_redis):
    hub = WebchatHub(bytes_redis)
    await hub.start()
    task = hub._task
    await hub.start()
    assert hub._task is task
    await hub.stop()
    assert task is not None and task.cancelled()
    assert hub._task is None
    await hub.stop()  # detener dos veces no falla


class FlakyRedis:
    """La primera suscripción falla (Redis caído); las siguientes funcionan."""

    def __init__(self, redis: fakeredis.FakeAsyncRedis) -> None:
        self._redis = redis
        self.failures = 1

    def pubsub(self, **kwargs):
        pubsub = self._redis.pubsub(**kwargs)
        if self.failures:
            self.failures -= 1

            async def broken(*_args):
                raise RedisConnectionError("Redis no responde")

            pubsub.psubscribe = broken
        return pubsub


async def test_hub_se_reconecta_si_redis_falla(bytes_redis, caplog):
    caplog.set_level(logging.WARNING, logger="iamia.integrations.webchat.adapter")
    hub = WebchatHub(FlakyRedis(bytes_redis))
    await hub.start()
    try:
        await wait_subscribed(bytes_redis)  # se resuscribe después de esperar 1 s
        waiter = asyncio.create_task(hub.wait("sesion-1", max_wait=5))
        await asyncio.sleep(0)
        await WebchatPublisher(bytes_redis).publish("sesion-1")
        assert await asyncio.wait_for(waiter, timeout=3) is True
    finally:
        await hub.stop()
    assert "webchat_hub_reconnecting" in caplog.messages


def test_adaptador_sin_webhooks(bytes_redis):
    adapter = build_webchat_adapter(bytes_redis)
    assert adapter.channel is Channel.WEBCHAT
    assert not adapter.accepts_webhooks
    assert adapter.capabilities.max_text_chars == 8000
