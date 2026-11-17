"""Armado de adaptadores según la configuración (iamia/integrations/factory.py)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from iamia.channels.models import (
    Channel,
    DeliveryError,
    Recipient,
    SendContext,
    WebhookBatch,
)
from iamia.core.errors import WebhookAuthError
from iamia.integrations.factory import build_adapters, build_channel_registry, x_oauth_client, x_token_provider
from iamia.integrations.x.tokens import MemoryTokenStore, RefreshingAccessToken, StaticAccessToken
from iamia.settings import Settings

from .support import NOW, meta_signature_header, webhook_request

ALL_CHANNELS = "webchat,whatsapp,instagram,messenger,telegram,x,email,sms"


def settings(**overrides) -> Settings:
    # _env_file=None: la prueba no depende de un .env local.
    return Settings(_env_file=None, **{"channels_enabled": "webchat", **overrides})


@pytest.fixture
async def registry_of(bytes_redis):
    registries = []

    def build(config: Settings, token_store=None):
        registry = build_channel_registry(config, redis=bytes_redis, token_store=token_store)
        registries.append(registry)
        return registry

    yield build
    for registry in registries:
        await registry.aclose()


def test_solo_registra_los_canales_habilitados(registry_of):
    assert registry_of(settings(channels_enabled="webchat,telegram")).channels == (Channel.WEBCHAT, Channel.TELEGRAM)


def test_nombres_sin_distinguir_mayusculas_ni_espacios(registry_of):
    assert registry_of(settings(channels_enabled=" WhatsApp , SMS ,")).channels == (Channel.WHATSAPP, Channel.SMS)


def test_todos_los_canales(registry_of):
    config = settings(channels_enabled=ALL_CHANNELS, x_token_mode="static", x_user_access_token="test-token")
    registry = registry_of(config)
    assert [channel.value for channel in registry.channels] == ALL_CHANNELS.split(",")
    assert all(registry.get(channel).channel is channel for channel in registry.channels)


def test_ningun_canal(bytes_redis):
    assert build_adapters(settings(channels_enabled=""), redis=bytes_redis) == []


def test_canal_desconocido(bytes_redis):
    with pytest.raises(ValueError, match="canal desconocido"):
        build_adapters(settings(channels_enabled="webchat,fax"), redis=bytes_redis)


# ------------------------------------------------------------------ X: token fijo o renovable
async def test_x_modo_static_usa_el_token_fijo():
    provider = x_token_provider(settings(x_token_mode="static", x_user_access_token="test-token"), None)
    assert isinstance(provider, StaticAccessToken)
    assert await provider.access_token() == "test-token"


async def test_x_modo_refresh_usa_el_almacen_de_tokens():
    provider = x_token_provider(settings(x_token_mode="refresh", x_client_id="client-123"), MemoryTokenStore())
    assert isinstance(provider, RefreshingAccessToken)
    await provider.aclose()


def test_x_modo_refresh_sin_almacen_falla(bytes_redis):
    with pytest.raises(ValueError, match="almacén de tokens"):
        x_token_provider(settings(x_token_mode="refresh"), None)
    with pytest.raises(ValueError, match="almacén de tokens"):
        build_adapters(settings(channels_enabled="x", x_token_mode="refresh"), redis=bytes_redis)


def test_x_modo_refresh_con_almacen(registry_of):
    registry = registry_of(settings(channels_enabled="x", x_token_mode="refresh"), token_store=MemoryTokenStore())
    assert registry.channels == (Channel.X,)


def test_x_oauth_client():
    client = x_oauth_client(
        settings(
            x_client_id="client-123", x_client_secret="client-secret", x_token_url="https://api.example.test/token"
        )
    )
    assert (client.client_id, client.client_secret, client.token_url) == (
        "client-123",
        "client-secret",
        "https://api.example.test/token",
    )


# ------------------------------------------------------------------ cada canal recibe su configuración
def _subscribe(verify_token: str):
    query = {"hub.mode": "subscribe", "hub.verify_token": verify_token, "hub.challenge": "1158201444"}
    return webhook_request(method="GET", query=query)


def test_instagram_y_messenger_usan_cada_uno_su_configuracion(registry_of):
    config = settings(
        channels_enabled="instagram,messenger",
        instagram_account_id="17841400000000000",
        instagram_app_secret="ig-secret",
        instagram_verify_token="ig-verify",
        instagram_human_agent_tag=True,
        messenger_page_id="123456",
        messenger_app_secret="fb-secret",
        messenger_verify_token="fb-verify",
        messenger_human_agent_tag=False,
    )
    registry = registry_of(config)
    instagram, messenger = registry.get(Channel.INSTAGRAM), registry.get(Channel.MESSENGER)

    body = b'{"object": "instagram", "entry": []}'
    signed = webhook_request(body, headers={"x-hub-signature-256": meta_signature_header("ig-secret", body)})
    instagram.verifier.authenticate(signed)
    with pytest.raises(WebhookAuthError):
        messenger.verifier.authenticate(signed)

    assert instagram.verifier.challenge(_subscribe("ig-verify")) is not None
    assert messenger.verifier.challenge(_subscribe("ig-verify")) is None
    assert messenger.verifier.challenge(_subscribe("fb-verify")) is not None

    # Cada uno escucha su objeto de webhook.
    assert instagram.parser.parse(webhook_request(b'{"object": "page", "entry": []}')) == WebhookBatch(ignored=1)
    assert messenger.parser.parse(webhook_request(b'{"object": "page", "entry": []}')) == WebhookBatch()

    # HUMAN_AGENT solo donde está habilitado.
    two_days = Recipient(Channel.INSTAGRAM, "IGSID", "17841400000000000", last_inbound_at=NOW - timedelta(days=2))
    operator = SendContext(now=NOW, operator_reply=True)
    assert instagram.sender.messaging_type(two_days, operator) == {
        "messaging_type": "MESSAGE_TAG",
        "tag": "HUMAN_AGENT",
    }
    with pytest.raises(DeliveryError):
        messenger.sender.messaging_type(two_days, operator)


def test_whatsapp_filtra_por_su_numero(registry_of):
    whatsapp = registry_of(settings(channels_enabled="whatsapp", whatsapp_phone_number_id="123456")).get(
        Channel.WHATSAPP
    )
    value = {
        "metadata": {"phone_number_id": "999999"},
        "messages": [{"from": "5491122334455", "id": "wamid.1", "type": "text"}],
    }
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": value}]}]}
    assert whatsapp.parser.parse(webhook_request(payload)) == WebhookBatch(ignored=1)


def test_telegram_usa_su_secreto(registry_of):
    telegram = registry_of(settings(channels_enabled="telegram", telegram_webhook_secret="tg-secret")).get(
        Channel.TELEGRAM
    )
    telegram.verifier.authenticate(webhook_request(b"{}", headers={"x-telegram-bot-api-secret-token": "tg-secret"}))
    with pytest.raises(WebhookAuthError):
        telegram.verifier.authenticate(webhook_request(b"{}", headers={"x-telegram-bot-api-secret-token": "otro"}))


def test_sms_responde_twiml(registry_of):
    sms = registry_of(settings(channels_enabled="sms")).get(Channel.SMS)
    assert sms.webhook_reply is not None
    assert sms.webhook_reply.media_type == "application/xml"
