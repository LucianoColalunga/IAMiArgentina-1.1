"""Cifrado de campos sensibles y seudonimización.

- FieldCipher: AES-256-GCM con anillo de claves. La primera clave cifra; todas descifran, así
  se puede rotar sin migrar los datos de golpe (el mantenimiento recifra en segundo plano).
- Pseudonymizer: HMAC-SHA256 con clave secreta. El mismo dato produce siempre el mismo
  seudónimo (sirve para búsquedas e índices únicos) y sin la clave no se puede revertir.

Formato de la configuración DATA_ENCRYPTION_KEYS: "k2:<base64 de 32 bytes>,k1:<...>".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ID = re.compile(r"^[a-z0-9]{1,16}$")
_FORMAT_VERSION = "v1"


class KeyConfigError(ValueError):
    """La configuración de claves es inválida."""


class DecryptionError(ValueError):
    """El dato no se pudo descifrar (clave desconocida o dato alterado)."""


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_key() -> str:
    """Clave nueva en base64 (32 bytes aleatorios) para DATA_ENCRYPTION_KEYS o PSEUDONYMIZATION_KEY."""
    return _b64encode(secrets.token_bytes(32))


class FieldCipher:
    def __init__(self, keys: Mapping[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise KeyConfigError("la clave activa no está en el anillo")
        for key_id, key in keys.items():
            if not _KEY_ID.match(key_id):
                raise KeyConfigError(f"identificador de clave inválido: {key_id!r}")
            if len(key) != 32:
                raise KeyConfigError(f"la clave {key_id} debe tener 32 bytes (AES-256)")
        self._keys = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self._active = active_key_id

    @classmethod
    def from_config(cls, value: str) -> FieldCipher:
        keys: dict[str, bytes] = {}
        order: list[str] = []
        for item in (part.strip() for part in value.split(",")):
            if not item:
                continue
            key_id, sep, encoded = item.partition(":")
            if not sep:
                raise KeyConfigError("formato esperado: id:base64,id:base64")
            try:
                keys[key_id.strip()] = _b64decode(encoded.strip())
            except (binascii.Error, ValueError) as exc:
                raise KeyConfigError(f"la clave {key_id} no es base64 válido") from exc
            order.append(key_id.strip())
        if not order:
            raise KeyConfigError("no hay claves configuradas")
        return cls(keys, order[0])

    @property
    def active_key_id(self) -> str:
        return self._active

    @property
    def key_ids(self) -> tuple[str, ...]:
        """Todas las claves del anillo, la activa incluida."""
        return tuple(self._keys)

    def encrypt(self, plaintext: str, *, associated_data: str = "") -> str:
        nonce = os.urandom(12)
        ciphertext = self._keys[self._active].encrypt(nonce, plaintext.encode("utf-8"), associated_data.encode("utf-8"))
        return f"{_FORMAT_VERSION}:{self._active}:{_b64encode(nonce + ciphertext)}"

    def decrypt(self, token: str, *, associated_data: str = "") -> str:
        try:
            version, key_id, payload = token.split(":", 2)
        except ValueError as exc:
            raise DecryptionError("formato de dato cifrado inválido") from exc
        if version != _FORMAT_VERSION or key_id not in self._keys:
            raise DecryptionError("versión o clave desconocida")
        raw = _b64decode(payload)
        try:
            plaintext = self._keys[key_id].decrypt(raw[:12], raw[12:], associated_data.encode("utf-8"))
        except InvalidTag as exc:
            raise DecryptionError("el dato fue alterado o la clave no corresponde") from exc
        return plaintext.decode("utf-8")

    def needs_rotation(self, token: str) -> bool:
        parts = token.split(":", 2)
        return len(parts) != 3 or parts[1] != self._active

    def reencrypt(self, token: str, *, associated_data: str = "") -> str:
        """Descifra con la clave que corresponda y vuelve a cifrar con la activa (mismos AAD)."""
        return self.encrypt(self.decrypt(token, associated_data=associated_data), associated_data=associated_data)


class Pseudonymizer:
    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise KeyConfigError("la clave de seudonimización debe tener al menos 16 bytes")
        self._key = key

    @classmethod
    def from_config(cls, value: str) -> Pseudonymizer:
        try:
            return cls(_b64decode(value.strip()))
        except (binascii.Error, ValueError) as exc:
            raise KeyConfigError("PSEUDONYMIZATION_KEY no es base64 válido") from exc

    def pseudonym(self, *parts: str) -> str:
        message = "\x1f".join(parts).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()
