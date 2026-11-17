"""Cifrado y seudonimización de los datos personales que se guardan.

- Dirección del contacto (teléfono, id de plataforma, correo): cifrada con AES-256-GCM y, para
  buscarla, un HMAC por canal. Los datos asociados (AAD) atan cada cifrado a su fila: un valor
  copiado a otra fila no se descifra.
- Cuerpo de los mensajes: los entrantes se guardan con los datos personales enmascarados y
  además cifrados; los salientes, cifrados.
"""

from __future__ import annotations

import json
from typing import Any

from iamia.channels.models import Channel, OutboundContent, SourceLink
from iamia.security.crypto import FieldCipher, Pseudonymizer
from iamia.security.pii import mask_pii


class DataCodec:
    def __init__(self, cipher: FieldCipher, pseudonymizer: Pseudonymizer) -> None:
        self._cipher = cipher
        self._pseudonymizer = pseudonymizer

    # ----------------------------------------------------------------- contactos
    def address_hash(self, channel: Channel | str, address: str) -> str:
        return self._pseudonymizer.pseudonym("address", str(channel), address)

    def encrypt_address(self, channel: Channel | str, address_hash: str, address: str) -> str:
        return self._cipher.encrypt(address, associated_data=f"contact:{channel}:{address_hash}")

    def decrypt_address(self, channel: Channel | str, address_hash: str, token: str) -> str:
        return self._cipher.decrypt(token, associated_data=f"contact:{channel}:{address_hash}")

    def link_code_hash(self, code: str) -> str:
        return self._pseudonymizer.pseudonym("link", code.upper())

    def token_hash(self, *parts: str) -> str:
        return self._pseudonymizer.pseudonym(*parts)

    # ----------------------------------------------------------------- mensajes
    def encrypt_inbound(self, message_id: str, text: str | None) -> str | None:
        if not text:
            return None
        masked, _ = mask_pii(text)
        return self._cipher.encrypt(masked, associated_data=f"message:{message_id}")

    def decrypt_text(self, message_id: str, token: str | None) -> str | None:
        if not token:
            return None
        return self._cipher.decrypt(token, associated_data=f"message:{message_id}")

    def encrypt_content(self, message_id: str, content: OutboundContent) -> str:
        payload: dict[str, Any] = {
            "text": content.text,
            "sources": [{"title": source.title, "url": source.url} for source in content.sources],
        }
        return self._cipher.encrypt(json.dumps(payload, ensure_ascii=False), associated_data=f"message:{message_id}")

    def decrypt_content(self, message_id: str, token: str | None) -> OutboundContent:
        raw = self.decrypt_text(message_id, token)
        if not raw:
            return OutboundContent("")
        try:
            payload = json.loads(raw)
        except ValueError:
            return OutboundContent(raw)
        sources = tuple(
            SourceLink(str(item.get("title", "")), str(item.get("url", "")))
            for item in payload.get("sources", [])
            if isinstance(item, dict)
        )
        return OutboundContent(str(payload.get("text", "")), sources)

    # ----------------------------------------------------------------- secretos de integraciones
    def encrypt_secret(self, purpose: str, value: str) -> str:
        return self._cipher.encrypt(value, associated_data=f"secret:{purpose}")

    def decrypt_secret(self, purpose: str, token: str) -> str:
        return self._cipher.decrypt(token, associated_data=f"secret:{purpose}")

    # ----------------------------------------------------------------- rotación de claves
    @property
    def active_key_id(self) -> str:
        return self._cipher.active_key_id

    @property
    def key_ids(self) -> tuple[str, ...]:
        return self._cipher.key_ids

    def needs_rotation(self, token: str) -> bool:
        return self._cipher.needs_rotation(token)

    def rotate_address(self, channel: Channel | str, address_hash: str, token: str) -> str:
        return self._cipher.reencrypt(token, associated_data=f"contact:{channel}:{address_hash}")

    def rotate_text(self, message_id: str, token: str) -> str:
        return self._cipher.reencrypt(token, associated_data=f"message:{message_id}")

    def rotate_secret(self, purpose: str, token: str) -> str:
        return self._cipher.reencrypt(token, associated_data=f"secret:{purpose}")
