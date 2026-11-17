"""Excepciones del dominio.

Las capas internas (motor de IA, canales, conversaciones) no conocen HTTP: lanzan estas
excepciones y cada borde (API, workers) decide cómo traducirlas.
"""

from __future__ import annotations


class IamiaError(Exception):
    """Base de los errores propios de la plataforma."""


class InvalidRequest(IamiaError):
    """El pedido no es válido (vacío, demasiado largo, formato incorrecto)."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class RetryableError(IamiaError):
    """Falla transitoria: tiene sentido reintentar más tarde."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceUnavailable(RetryableError):
    """Una dependencia (modelo, embeddings, base) no responde."""


class ServiceBusy(RetryableError):
    """Hay capacidad, pero está saturada en este momento (backpressure)."""


class QuotaExceeded(IamiaError):
    """El cliente agotó su cupo (rate limit o presupuesto diario de tokens)."""

    def __init__(self, user_message: str, retry_after: int) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.retry_after = retry_after


class WebhookAuthError(IamiaError):
    """Firma o secreto del webhook inválido."""


class WebhookPayloadError(IamiaError):
    """El cuerpo del webhook no tiene el formato esperado."""


class ChannelDisabled(IamiaError):
    """El canal no está habilitado en esta instalación."""


class NotFound(IamiaError):
    pass


class Conflict(IamiaError):
    pass


class Unauthorized(IamiaError):
    pass


class Forbidden(IamiaError):
    pass
