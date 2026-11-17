"""Autenticación de la API de administración con tokens OIDC (OAuth 2.0 / JWT).

En producción los tokens los emite el proveedor de identidad de la organización (por ejemplo
Keycloak) y las claves públicas se obtienen de su JWKS. En desarrollo se puede configurar una
clave pública PEM local (ver `python -m iamia dev-token`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jwt

from iamia.core.errors import Unauthorized
from iamia.security.rbac import Principal, parse_roles

logger = logging.getLogger(__name__)


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


@dataclass(frozen=True)
class OidcConfig:
    audience: str
    algorithms: list[str]
    issuer: str = ""
    jwks_url: str = ""
    public_key_pem: str = ""
    roles_claim: str = "realm_access.roles"
    leeway_seconds: int = 30


def _claim(claims: dict[str, Any], path: str) -> Any:
    """Lee un claim anidado: "realm_access.roles" -> claims["realm_access"]["roles"]."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class JwtVerifier:
    def __init__(self, config: OidcConfig) -> None:
        if not config.jwks_url and not config.public_key_pem:
            raise ValueError("se necesita jwks_url o public_key_pem")
        self._config = config
        self._jwks = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=300) if config.jwks_url else None

    @classmethod
    def from_settings(
        cls, *, issuer: str, audience: str, algorithms: list[str], jwks_url: str, public_key_path: str, roles_claim: str
    ) -> JwtVerifier:
        pem = Path(public_key_path).read_text(encoding="utf-8") if public_key_path and not jwks_url else ""
        return cls(
            OidcConfig(
                audience=audience,
                algorithms=algorithms,
                issuer=issuer,
                jwks_url=jwks_url,
                public_key_pem=pem,
                roles_claim=roles_claim,
            )
        )

    def verify(self, token: str) -> Principal:
        config = self._config
        try:
            key: Any = self._jwks.get_signing_key_from_jwt(token).key if self._jwks else config.public_key_pem
            options: dict[str, Any] = {"require": ["exp", "iat", "sub"]}
            claims = jwt.decode(
                token,
                key,
                algorithms=config.algorithms,
                audience=config.audience,
                issuer=config.issuer or None,
                leeway=config.leeway_seconds,
                options=options,
            )
        except jwt.PyJWTError as exc:
            logger.info("token_rejected", extra={"ctx": {"reason": type(exc).__name__}})
            raise Unauthorized("token inválido") from exc
        roles = parse_roles(_claim(claims, config.roles_claim))
        name = claims.get("preferred_username") or claims.get("name")
        return Principal(subject=str(claims["sub"]), roles=roles, name=str(name) if name else None)
