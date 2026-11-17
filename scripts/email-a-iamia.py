#!/usr/bin/env python3
"""Entrega un correo entrante a IAMiArgentina.

El servidor de correo de la organización (Postfix u otro) pasa cada mensaje por la entrada
estándar y este script lo publica firmado en POST /webhooks/email. Usa solo la biblioteca
estándar de Python 3, así que no hay que instalar nada en el servidor de correo.

Postfix (master.cf), como transporte de la dirección del asistente:

    iamia  unix  -  n  n  -  4  pipe
      flags=Rq user=iamia-mail argv=/usr/local/bin/email-a-iamia.py

y en transport_maps:   asistente@ejemplo.org   iamia:

Configuración por entorno o en /etc/default/email-a-iamia (líneas CLAVE=valor):

    IAMIA_EMAIL_URL           URL pública de /webhooks/email
    IAMIA_EMAIL_SECRET_FILE   archivo con el valor de EMAIL_INBOUND_SECRET (permisos 0400)

Códigos de salida (los que entiende Postfix): 0 entregado; 75 falla temporal y Postfix reintenta
más tarde; 65 el correo es inválido y no tiene sentido reintentarlo.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.request
from email import message_from_bytes, policy
from email.message import EmailMessage

EX_OK, EX_DATAERR, EX_TEMPFAIL = 0, 65, 75
MAX_BYTES = 3 * 1024 * 1024 - 64 * 1024  # la API acepta hasta 3 MB por pedido
CONFIG_FILE = "/etc/default/email-a-iamia"


def load_config() -> dict[str, str]:
    config = dict(os.environ)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    config.setdefault(key.strip(), value.strip().strip('"'))
    return config


def without_attachments(raw: bytes) -> bytes:
    """El asistente solo lee el texto: si el correo es muy grande, los adjuntos viajan vacíos."""
    message = message_from_bytes(raw, policy=policy.default)
    if not isinstance(message, EmailMessage):
        return raw
    for part in message.walk():
        if part.is_attachment():
            maintype, _, subtype = part.get_content_type().partition("/")
            part.set_content(b"", maintype=maintype, subtype=subtype, filename=part.get_filename())
    return message.as_bytes()


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def main() -> int:
    config = load_config()
    url, secret_file = config.get("IAMIA_EMAIL_URL"), config.get("IAMIA_EMAIL_SECRET_FILE")
    if not url or not secret_file:
        print("email-a-iamia: falta IAMIA_EMAIL_URL o IAMIA_EMAIL_SECRET_FILE", file=sys.stderr)
        return EX_TEMPFAIL
    try:
        with open(secret_file, "rb") as handle:
            secret = handle.read().strip()
    except OSError as exc:
        print(f"email-a-iamia: no se pudo leer el secreto: {exc.strerror}", file=sys.stderr)
        return EX_TEMPFAIL

    body = sys.stdin.buffer.read()
    if len(body) > MAX_BYTES:
        body = without_attachments(body)
        if len(body) > MAX_BYTES:
            print("email-a-iamia: el correo es demasiado grande aun sin adjuntos", file=sys.stderr)
            return EX_DATAERR

    if not url.startswith(("https://", "http://")):
        print("email-a-iamia: IAMIA_EMAIL_URL tiene que ser una URL http(s)", file=sys.stderr)
        return EX_TEMPFAIL
    timestamp = str(int(time.time()))
    request = urllib.request.Request(  # noqa: S310 - esquema validado arriba
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "message/rfc822",
            "X-IAMIA-Timestamp": timestamp,
            "X-IAMIA-Signature": sign(secret, timestamp, body),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - esquema validado arriba
            return EX_OK if 200 <= response.status < 300 else EX_TEMPFAIL
    except urllib.error.HTTPError as exc:
        print(f"email-a-iamia: IAMiArgentina respondió {exc.code}", file=sys.stderr)
        # 401 suele ser un secreto mal copiado o el reloj desfasado: se reintenta mientras se corrige.
        return EX_DATAERR if exc.code in (400, 413, 415, 422) else EX_TEMPFAIL
    except (urllib.error.URLError, OSError) as exc:
        print(f"email-a-iamia: sin conexión con IAMiArgentina: {exc}", file=sys.stderr)
        return EX_TEMPFAIL


if __name__ == "__main__":
    sys.exit(main())
