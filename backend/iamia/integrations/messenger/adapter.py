"""Messenger (páginas de Facebook).

Se envía con el token de la página a /{page-id}/messages. Desde abril de 2026 las etiquetas
CONFIRMED_EVENT_UPDATE, ACCOUNT_UPDATE y POST_PURCHASE_UPDATE devuelven error: fuera de la
ventana de 24 h solo queda HUMAN_AGENT (operadores, hasta 7 días). El webhook tiene que
contestar 200 en menos de 5 segundos; si falla durante una hora Meta lo desactiva, por eso la
API solo valida y encola.
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

MAX_TEXT_CHARS = 1990  # la Send API exige menos de 2000 caracteres
CAPABILITIES = ChannelCapabilities(
    max_text_chars=MAX_TEXT_CHARS,
    reply_window=timedelta(hours=24),
    human_agent_window=timedelta(days=7),
    supports_typing=True,
)


def build_messenger_adapter(config: MetaMessagingConfig, http: ProviderHttp | None = None) -> ChannelAdapter:
    if config.channel is not Channel.MESSENGER or config.webhook_object != "page":
        raise ValueError("configuración de Messenger inválida")
    webhook = MetaMessagingWebhook(config)
    sender = MetaMessagingSender(config, build_graph_client(config, http))
    return ChannelAdapter(
        channel=Channel.MESSENGER,
        capabilities=CAPABILITIES,
        formatter=PlainTextFormatter(MAX_TEXT_CHARS),
        sender=sender,
        verifier=webhook,
        parser=webhook,
        typing=sender,
    )
