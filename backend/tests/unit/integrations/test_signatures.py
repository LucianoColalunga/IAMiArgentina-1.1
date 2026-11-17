"""Firmas de webhooks (iamia/security/signatures.py)."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from iamia.security.signatures import (
    constant_time_equals,
    hmac_sha256_b64,
    hmac_sha256_hex,
    meta_signature,
    timestamped_signature,
    twilio_signature,
    verify_meta_signature,
    verify_timestamped_signature,
    verify_twilio_signature,
    verify_x_signature,
    x_crc_response_token,
    x_signature,
)

from .support import meta_signature_header, twilio_signature_header, x_signature_header

BODY = b'{"object":"page","entry":[]}'
TS = 1_763_294_400


def test_constant_time_equals():
    assert constant_time_equals("abc", "abc")
    assert constant_time_equals("ñandú", "ñandú")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")


def test_hmac_sha256_vector_rfc_4231():
    # Caso de prueba 2 de la RFC 4231.
    expected = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    assert hmac_sha256_hex("Jefe", b"what do ya want for nothing?") == expected
    assert (
        hmac_sha256_b64("Jefe", b"what do ya want for nothing?") == base64.b64encode(bytes.fromhex(expected)).decode()
    )


# ------------------------------------------------------------------------- Meta
def test_meta_signature_valida():
    header = meta_signature_header("app-secret", BODY)
    assert meta_signature("app-secret", BODY) == header
    assert verify_meta_signature("app-secret", BODY, header)
    assert verify_meta_signature("app-secret", BODY, f"  {header}\n")


@pytest.mark.parametrize(
    ("secret", "body", "header"),
    [
        ("otro-secreto", BODY, meta_signature_header("app-secret", BODY)),
        ("app-secret", BODY + b" ", meta_signature_header("app-secret", BODY)),
        ("app-secret", BODY, None),
        ("app-secret", BODY, ""),
        ("app-secret", BODY, meta_signature_header("app-secret", BODY).removeprefix("sha256=")),
        ("app-secret", BODY, "sha1=" + hmac.new(b"app-secret", BODY, hashlib.sha1).hexdigest()),
        ("", BODY, meta_signature_header("", BODY)),
    ],
    ids=["otro-secreto", "cuerpo-alterado", "sin-header", "header-vacio", "sin-prefijo", "sha1", "sin-secreto"],
)
def test_meta_signature_rechaza(secret, body, header):
    assert not verify_meta_signature(secret, body, header)


# ------------------------------------------------------------------------- X
def test_x_crc_response_token_es_base64_del_hmac_del_token():
    expected = "sha256=" + base64.b64encode(hmac.new(b"x-secret", b"crc-123", hashlib.sha256).digest()).decode()
    assert x_crc_response_token("x-secret", "crc-123") == expected


def test_x_signature():
    header = x_signature_header("x-secret", BODY)
    assert x_signature("x-secret", BODY) == header
    assert verify_x_signature("x-secret", BODY, header)
    assert not verify_x_signature("x-secret", BODY + b" ", header)
    assert not verify_x_signature("otro-secreto", BODY, header)
    assert not verify_x_signature("x-secret", BODY, None)
    assert not verify_x_signature("", BODY, x_signature_header("", BODY))


# ------------------------------------------------------------------------- Twilio
TWILIO_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}


def test_twilio_signature_ejemplo_de_la_documentacion():
    # Ejemplo publicado por Twilio (auth token "12345").
    assert twilio_signature("12345", TWILIO_URL, TWILIO_PARAMS.items()) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert twilio_signature_header("12345", TWILIO_URL, TWILIO_PARAMS) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_twilio_signature_no_depende_del_orden_de_los_parametros():
    shuffled = list(reversed(TWILIO_PARAMS.items()))
    assert twilio_signature("12345", TWILIO_URL, shuffled) == twilio_signature(
        "12345", TWILIO_URL, TWILIO_PARAMS.items()
    )


def test_verify_twilio_signature():
    header = twilio_signature_header("12345", TWILIO_URL, TWILIO_PARAMS)
    params = list(TWILIO_PARAMS.items())
    assert verify_twilio_signature("12345", TWILIO_URL, params, header)
    assert not verify_twilio_signature("12345", TWILIO_URL, [*params, ("Body", "hola")], header)
    assert not verify_twilio_signature("12345", "https://mycompany.com/otra", params, header)
    assert not verify_twilio_signature("otro-token", TWILIO_URL, params, header)
    assert not verify_twilio_signature("", TWILIO_URL, params, header)
    assert not verify_twilio_signature("12345", "", params, header)
    assert not verify_twilio_signature("12345", TWILIO_URL, params, None)


# ------------------------------------------------------------------------- firma propia con timestamp
def _v1(secret: str, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def test_timestamped_signature_formato():
    assert timestamped_signature("mail-secret", str(TS), b"cuerpo") == _v1("mail-secret", str(TS), b"cuerpo")


@pytest.mark.parametrize("offset", [0, 300, -300])
def test_timestamped_signature_dentro_de_la_tolerancia(offset):
    signature = _v1("mail-secret", str(TS), b"cuerpo")
    assert verify_timestamped_signature("mail-secret", b"cuerpo", str(TS), signature, now=TS + offset)


@pytest.mark.parametrize("offset", [301, -301])
def test_timestamped_signature_vencida_o_del_futuro(offset):
    signature = _v1("mail-secret", str(TS), b"cuerpo")
    assert not verify_timestamped_signature("mail-secret", b"cuerpo", str(TS), signature, now=TS + offset)


def test_timestamped_signature_tolerancia_configurable():
    signature = _v1("mail-secret", str(TS), b"cuerpo")
    assert not verify_timestamped_signature(
        "mail-secret", b"cuerpo", str(TS), signature, now=TS + 61, tolerance_seconds=60
    )


@pytest.mark.parametrize(
    ("secret", "body", "timestamp", "signature"),
    [
        ("mail-secret", b"otro cuerpo", str(TS), _v1("mail-secret", str(TS), b"cuerpo")),
        ("otro-secreto", b"cuerpo", str(TS), _v1("mail-secret", str(TS), b"cuerpo")),
        ("mail-secret", b"cuerpo", str(TS + 1), _v1("mail-secret", str(TS), b"cuerpo")),
        ("mail-secret", b"cuerpo", "1.7e9", _v1("mail-secret", "1.7e9", b"cuerpo")),
        ("mail-secret", b"cuerpo", None, _v1("mail-secret", str(TS), b"cuerpo")),
        ("mail-secret", b"cuerpo", str(TS), None),
        ("", b"cuerpo", str(TS), _v1("", str(TS), b"cuerpo")),
    ],
    ids=[
        "cuerpo-alterado",
        "otro-secreto",
        "otro-timestamp",
        "timestamp-no-numerico",
        "sin-timestamp",
        "sin-firma",
        "sin-secreto",
    ],
)
def test_timestamped_signature_rechaza(secret, body, timestamp, signature):
    assert not verify_timestamped_signature(secret, body, timestamp, signature, now=TS)
