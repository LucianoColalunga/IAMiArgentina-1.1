"""Registro de canales habilitados."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from iamia.channels.contracts import AsyncClosable, ChannelAdapter
from iamia.channels.models import Channel
from iamia.core.errors import ChannelDisabled

logger = logging.getLogger(__name__)


class ChannelRegistry:
    def __init__(self, adapters: Iterable[ChannelAdapter]) -> None:
        self._adapters: dict[Channel, ChannelAdapter] = {}
        for adapter in adapters:
            if adapter.channel in self._adapters:
                raise ValueError(f"canal registrado dos veces: {adapter.channel}")
            self._adapters[adapter.channel] = adapter

    @property
    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._adapters)

    def get(self, channel: Channel) -> ChannelAdapter:
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise ChannelDisabled(f"el canal {channel} no está habilitado") from exc

    def find(self, name: str) -> ChannelAdapter | None:
        try:
            return self._adapters.get(Channel.parse(name))
        except ValueError:
            return None

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            for component in {id(adapter.sender): adapter.sender, id(adapter.typing): adapter.typing}.values():
                if isinstance(component, AsyncClosable):
                    try:
                        await component.aclose()
                    except Exception:
                        logger.exception("channel_close_error", extra={"ctx": {"channel": adapter.channel.value}})
