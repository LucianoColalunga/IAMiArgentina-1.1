"""Instagram Direct con "Instagram API with Unified Login".

Es la configuración que Meta recomienda para integraciones nuevas (las apps nuevas ya no pueden
usar "Instagram Login"). Se usa graph.facebook.com, un token de System User del portfolio de
negocio y el endpoint /{ig-user-id}/messages. Requisitos de la cuenta: profesional, con "Permitir
acceso a los mensajes" activado, y la app en modo Live con acceso avanzado para atender a
cualquier persona.

El texto admite como máximo 1000 bytes UTF-8 (no caracteres): las tildes ocupan dos bytes.
"""

from __future__ import annotations

from datetime import timedelta

from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import Channel
from iamia.integrations.http import ProviderHttp
from iamia.integrations.meta.messaging import (
    MetaMessagingConfig,
    MetaMessagingSender,
    MetaMessagingWebhook,
    build_graph_client,
)

MAX_TEXT_BYTES = 1000
CAPABILITIES = ChannelCapabilities(
    max_text_chars=MAX_TEXT_BYTES,
    reply_window=timedelta(hours=24),
    human_agent_window=timedelta(days=7),
    supports_typing=True,
)


def build_instagram_adapter(config: MetaMessagingConfig, http: ProviderHttp | None = None) -> ChannelAdapter:
    if config.channel is not Channel.INSTAGRAM or config.webhook_object != "instagram":
        raise ValueError("configuración de Instagram inválida")
    webhook = MetaMessagingWebhook(config)
    sender = MetaMessagingSender(config, build_graph_client(config, http))
    return ChannelAdapter(
        channel=Channel.INSTAGRAM,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(MAX_TEXT_BYTES, max_bytes=MAX_TEXT_BYTES),
        sender=sender,
        verifier=webhook,
        parser=webhook,
        typing=sender,
    )
