"""Dependencias compartidas por los routers."""

from __future__ import annotations

import ipaddress

from fastapi import Request

from iamia.bootstrap import Container
from iamia.core.errors import Unauthorized
from iamia.security.auth import TokenVerifier
from iamia.security.rbac import Principal
from iamia.security.signatures import constant_time_equals


def container(request: Request) -> Container:
    return request.app.state.container


def client_ip(request: Request) -> str:
    """IP del cliente. Detrás del proxy propio se confía en X-Real-IP (lo fija nginx)."""
    settings = container(request).settings
    if settings.trust_proxy_headers:
        candidate = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def verify_edge(request: Request) -> None:
    """Si hay secreto de borde configurado, solo se aceptan pedidos que pasaron por el proxy."""
    secret = container(request).settings.edge_shared_secret.get_secret_value()
    if secret and not constant_time_equals(request.headers.get("x-edge-auth", ""), secret):
        raise Unauthorized("pedido sin autenticación de borde")


def current_principal(request: Request) -> Principal:
    verifier: TokenVerifier | None = request.app.state.token_verifier
    if verifier is None:
        raise Unauthorized("la autenticación de operadores no está configurada")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("falta el token de acceso")
    return verifier.verify(token.strip())
