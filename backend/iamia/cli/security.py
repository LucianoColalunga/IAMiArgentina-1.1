"""Comandos de seguridad: revisión de configuración, generación de claves y autorizaciones."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from iamia.core.clock import utcnow
from iamia.security.crypto import generate_key
from iamia.security.rbac import Role
from iamia.settings import get_settings


def check_config(_: argparse.Namespace) -> int:
    settings = get_settings()
    problems = settings.security_problems()
    print(f"entorno: {settings.app_env}")
    print(f"canales habilitados: {', '.join(settings.channels_enabled_list) or 'ninguno'}")
    if not problems:
        print("configuración de seguridad: sin observaciones")
        return 0
    for problem in problems:
        print(f"- {problem}")
    # En producción estos problemas impiden arrancar (ver bootstrap.check_security).
    return 1 if settings.is_production else 0


def gen_secrets(_: argparse.Namespace) -> int:
    """Imprime claves nuevas para pegar en el .env o en el gestor de secretos. No se guardan en disco."""
    key_id = "k" + utcnow().strftime("%Y%m%d")
    values = {
        "DATA_ENCRYPTION_KEYS": f"{key_id}:{generate_key()}",
        "PSEUDONYMIZATION_KEY": generate_key(),
        "WEBCHAT_TOKEN_SECRET": secrets.token_urlsafe(32),
        "EDGE_SHARED_SECRET": secrets.token_urlsafe(32),
        "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "WHATSAPP_VERIFY_TOKEN": secrets.token_hex(24),
        "INSTAGRAM_VERIFY_TOKEN": secrets.token_hex(24),
        "MESSENGER_VERIFY_TOKEN": secrets.token_hex(24),
        "EMAIL_INBOUND_SECRET": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "RABBITMQ_PASSWORD": secrets.token_urlsafe(24),
        "REDIS_PASSWORD": secrets.token_urlsafe(24),
    }
    for name, value in values.items():
        print(f"{name}={value}")
    print(
        "# Guardar en el gestor de secretos. Si se rota DATA_ENCRYPTION_KEYS, la clave nueva va primero y",
        "# las anteriores se conservan detrás (k2:...,k1:...) hasta que el mantenimiento recifre",
        "# (confirmarlo con: python -m iamia key-usage).",
        sep="\n",
    )
    return 0


def _nested(path: str, value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    cursor = result
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return result


def _dev_keys(key_dir: Path) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_path, public_path = key_dir / "oidc-dev-private.pem", key_dir / "oidc-dev-public.pem"
    if not private_path.exists():
        key_dir.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        os.chmod(private_path, 0o600)
        public_path.write_bytes(
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        print(f"claves de desarrollo creadas en {key_dir} (OIDC_PUBLIC_KEY_PATH={public_path})", file=sys.stderr)
    return private_path.read_bytes()


def dev_token(args: argparse.Namespace) -> int:
    """Emite un JWT firmado con una clave local para probar la consola sin un proveedor OIDC."""
    import jwt

    settings = get_settings()
    if settings.is_production:
        print("dev-token no está disponible en producción: los tokens los emite el proveedor OIDC.", file=sys.stderr)
        return 2
    roles = args.role or [Role.OPERATOR.value]
    valid = {role.value for role in Role}
    if any(role not in valid for role in roles):
        print(f"roles válidos: {', '.join(sorted(valid))}", file=sys.stderr)
        return 2
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": args.subject,
        "preferred_username": args.subject,
        "aud": settings.oidc_audience,
        "iat": now,
        "exp": now + max(1, args.minutes) * 60,
        **_nested(settings.oidc_roles_claim, roles),
    }
    if settings.oidc_issuer:
        claims["iss"] = settings.oidc_issuer
    print(jwt.encode(claims, _dev_keys(args.key_dir), algorithm="RS256"))
    return 0


def x_authorize(_: argparse.Namespace) -> int:
    """Autorización inicial de la cuenta de X. La hace una persona una sola vez; después el sistema
    renueva los tokens solo (ver integrations/x/tokens.py)."""
    from iamia.integrations.factory import x_oauth_client
    from iamia.integrations.x.tokens import build_authorization

    settings = get_settings()
    if not settings.x_client_id or not settings.x_oauth_redirect_uri:
        print("faltan X_CLIENT_ID y X_OAUTH_REDIRECT_URI", file=sys.stderr)
        return 2
    client = x_oauth_client(settings)
    request = build_authorization(client, settings.x_oauth_redirect_uri)
    print("1. Abrir esta dirección con la cuenta que va a responder los mensajes directos:\n")
    print(request.url)
    print("\n2. Después de autorizar, X redirige a la URL configurada. Pegar acá la URL completa:")
    redirected = input("> ").strip()
    query = parse_qs(urlsplit(redirected).query)
    if query.get("state", [""])[0] != request.state:
        print("el parámetro state no coincide: se cancela la autorización", file=sys.stderr)
        return 2
    code = query.get("code", [""])[0]
    if not code:
        print("la URL no trae el parámetro code", file=sys.stderr)
        return 2
    asyncio.run(_store_x_token(code, request.verifier))
    print("autorización guardada (cifrada) en la base de datos")
    return 0


async def _store_x_token(code: str, verifier: str) -> None:
    from iamia.channels.models import Channel
    from iamia.integrations.factory import x_oauth_client
    from iamia.integrations.http import ProviderHttp
    from iamia.integrations.x.tokens import exchange_code
    from iamia.persistence.codec import DataCodec
    from iamia.persistence.database import create_pool
    from iamia.persistence.oauth import PgTokenStore
    from iamia.security.crypto import FieldCipher, Pseudonymizer

    settings = get_settings()
    http = ProviderHttp(Channel.X, settings.channel_http_timeout_seconds)
    try:
        token = await exchange_code(
            http, x_oauth_client(settings), code=code, verifier=verifier, redirect_uri=settings.x_oauth_redirect_uri
        )
    finally:
        await http.aclose()
    codec = DataCodec(
        FieldCipher.from_config(settings.data_encryption_keys.get_secret_value()),
        Pseudonymizer.from_config(settings.pseudonymization_key.get_secret_value()),
    )
    pool = await create_pool(settings.database_url.get_secret_value(), min_size=1, max_size=1)
    try:
        await PgTokenStore(pool, codec).save("x", token)
    finally:
        await pool.close()
