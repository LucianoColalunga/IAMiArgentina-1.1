"""Genera tests/e2e/.env.e2e con secretos aleatorios para el entorno de punta a punta.

Son valores de prueba de un solo uso: se crean en cada máquina y nunca se versionan.
Se ejecuta dentro de la imagen del backend (necesita iamia para las claves de cifrado):

    docker run --rm -v "$PWD/tests/e2e:/out" iamia/backend:test python /out/make_env.py /out/.env.e2e
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from iamia.security.crypto import generate_key


def main(target: str) -> int:
    path = Path(target)
    if path.exists():
        print(f"{path} ya existe: se conserva")
        return 0
    values = {
        "DATA_ENCRYPTION_KEYS": f"k1:{generate_key()}",
        "PSEUDONYMIZATION_KEY": generate_key(),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(18),
        "REDIS_PASSWORD": secrets.token_urlsafe(18),
        "RABBITMQ_PASSWORD": secrets.token_urlsafe(18),
        "HTTP_PORT": "18080",
    }
    for name in (
        "EDGE_SHARED_SECRET",
        "WEBCHAT_TOKEN_SECRET",
        "TELEGRAM_WEBHOOK_SECRET",
        "WHATSAPP_APP_SECRET",
        "MESSENGER_APP_SECRET",
        "INSTAGRAM_APP_SECRET",
        "X_WEBHOOK_SECRET",
        "TWILIO_AUTH_TOKEN",
        "EMAIL_INBOUND_SECRET",
        "WHATSAPP_ACCESS_TOKEN",
        "MESSENGER_PAGE_ACCESS_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "X_USER_ACCESS_TOKEN",
    ):
        values[name] = secrets.token_urlsafe(32)
    for name in ("WHATSAPP_VERIFY_TOKEN", "MESSENGER_VERIFY_TOKEN", "INSTAGRAM_VERIFY_TOKEN"):
        values[name] = secrets.token_hex(16)
    values["TELEGRAM_BOT_TOKEN"] = f"{secrets.randbelow(10**9)}:{secrets.token_urlsafe(26)}"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    print(f"secretos de prueba generados en {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ".env.e2e"))
