"""Arma los adaptadores habilitados en CHANNELS_ENABLED a partir de la configuración.

Es el único lugar que conoce a la vez Settings y las clases concretas de cada integración
(composition root de los canales): el resto del sistema solo ve ChannelAdapter.
"""

from __future__ import annotations

from collections.abc import Callable

from redis.asyncio import Redis

from iamia.channels.contracts import ChannelAdapter
from iamia.channels.models import Channel
from iamia.channels.registry import ChannelRegistry
from iamia.integrations.email.adapter import EmailConfig, build_email_adapter
from iamia.integrations.http import ProviderHttp
from iamia.integrations.instagram.adapter import build_instagram_adapter
from iamia.integrations.messenger.adapter import build_messenger_adapter
from iamia.integrations.meta.messaging import MetaMessagingConfig
from iamia.integrations.sms.adapter import SmsConfig, build_sms_adapter
from iamia.integrations.telegram.adapter import TelegramConfig, build_telegram_adapter
from iamia.integrations.webchat.adapter import build_webchat_adapter
from iamia.integrations.whatsapp.adapter import WhatsAppConfig, build_whatsapp_adapter
from iamia.integrations.x.adapter import XConfig, build_x_adapter
from iamia.integrations.x.tokens import (
    AccessTokenProvider,
    OAuthClient,
    RefreshingAccessToken,
    StaticAccessToken,
    TokenStore,
)
from iamia.settings import Settings


def _secret(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if getter else str(value or "")


def x_oauth_client(settings: Settings) -> OAuthClient:
    return OAuthClient(
        client_id=settings.x_client_id,
        client_secret=_secret(settings.x_client_secret),
        token_url=settings.x_token_url,
        authorize_url=settings.x_authorize_url,
    )


def x_token_provider(settings: Settings, token_store: TokenStore | None) -> AccessTokenProvider:
    if settings.x_token_mode == "static":  # noqa: S105 - modo, no secreto
        return StaticAccessToken(_secret(settings.x_user_access_token))
    if token_store is None:
        raise ValueError("el canal X en modo refresh necesita un almacén de tokens")
    http = ProviderHttp(Channel.X, settings.channel_http_timeout_seconds)
    return RefreshingAccessToken(token_store, x_oauth_client(settings), http)


def _meta_messaging(settings: Settings, channel: Channel) -> MetaMessagingConfig:
    prefix = "instagram" if channel is Channel.INSTAGRAM else "messenger"
    return MetaMessagingConfig(
        channel=channel,
        webhook_object="instagram" if channel is Channel.INSTAGRAM else "page",
        account_id=settings.instagram_account_id if channel is Channel.INSTAGRAM else settings.messenger_page_id,
        access_token=_secret(
            settings.instagram_access_token if channel is Channel.INSTAGRAM else settings.messenger_page_access_token
        ),
        app_secret=_secret(getattr(settings, f"{prefix}_app_secret")),
        verify_token=_secret(getattr(settings, f"{prefix}_verify_token")),
        base_url=settings.meta_graph_base_url,
        api_version=settings.meta_graph_api_version,
        human_agent_tag=bool(getattr(settings, f"{prefix}_human_agent_tag")),
        appsecret_proof=settings.meta_appsecret_proof,
        timeout_seconds=settings.channel_http_timeout_seconds,
    )


def build_adapters(settings: Settings, *, redis: Redis, token_store: TokenStore | None = None) -> list[ChannelAdapter]:
    timeout = settings.channel_http_timeout_seconds
    builders: dict[str, Callable[[], ChannelAdapter]] = {
        "webchat": lambda: build_webchat_adapter(redis),
        "whatsapp": lambda: build_whatsapp_adapter(
            WhatsAppConfig(
                phone_number_id=settings.whatsapp_phone_number_id,
                access_token=_secret(settings.whatsapp_access_token),
                app_secret=_secret(settings.whatsapp_app_secret),
                verify_token=_secret(settings.whatsapp_verify_token),
                base_url=settings.meta_graph_base_url,
                api_version=settings.meta_graph_api_version,
                reengagement_template=settings.whatsapp_reengagement_template,
                template_language=settings.whatsapp_template_language,
                typing_indicator=settings.whatsapp_typing_indicator,
                appsecret_proof=settings.meta_appsecret_proof,
                timeout_seconds=timeout,
            )
        ),
        "instagram": lambda: build_instagram_adapter(_meta_messaging(settings, Channel.INSTAGRAM)),
        "messenger": lambda: build_messenger_adapter(_meta_messaging(settings, Channel.MESSENGER)),
        "telegram": lambda: build_telegram_adapter(
            TelegramConfig(
                bot_token=_secret(settings.telegram_bot_token),
                webhook_secret=_secret(settings.telegram_webhook_secret),
                api_base_url=settings.telegram_api_base_url,
                timeout_seconds=timeout,
            )
        ),
        "x": lambda: build_x_adapter(
            XConfig(
                account_id=settings.x_account_id,
                webhook_secret=_secret(settings.x_webhook_secret),
                api_base_url=settings.x_api_base_url,
                timeout_seconds=timeout,
            ),
            x_token_provider(settings, token_store),
        ),
        "email": lambda: build_email_adapter(
            EmailConfig(
                inbound_secret=_secret(settings.email_inbound_secret),
                from_address=settings.email_from_address,
                from_name=settings.email_from_name,
                smtp_host=settings.smtp_host,
                smtp_port=settings.smtp_port,
                smtp_username=settings.smtp_username,
                smtp_password=_secret(settings.smtp_password),
                smtp_starttls=settings.smtp_starttls,
                smtp_timeout_seconds=settings.smtp_timeout_seconds,
            )
        ),
        "sms": lambda: build_sms_adapter(
            SmsConfig(
                account_sid=settings.twilio_account_sid,
                auth_token=_secret(settings.twilio_auth_token),
                webhook_url=settings.sms_webhook_url,
                from_number=settings.twilio_from_number,
                messaging_service_sid=settings.twilio_messaging_service_sid,
                api_base_url=settings.twilio_api_base_url,
                max_segments=settings.sms_max_segments,
                timeout_seconds=timeout,
            )
        ),
    }
    adapters: list[ChannelAdapter] = []
    for name in settings.channels_enabled_list:
        builder = builders.get(name)
        if builder is None:
            raise ValueError(f"canal desconocido en CHANNELS_ENABLED: {name}")
        adapters.append(builder())
    return adapters


def build_channel_registry(
    settings: Settings, *, redis: Redis, token_store: TokenStore | None = None
) -> ChannelRegistry:
    return ChannelRegistry(build_adapters(settings, redis=redis, token_store=token_store))
