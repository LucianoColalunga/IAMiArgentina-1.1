"""Conteo y recorte de tokens.

Se usa el vocabulario cl100k_base de tiktoken como aproximación al tokenizer real del modelo
(Llama, Qwen y Mistral usan BPE de tamaño parecido). El error es chico frente a los márgenes que
dejamos en los límites. Si tiktoken no está disponible (entorno sin red) se usa una heurística
conservadora por caracteres.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 3.5  # conservador para español


class TokenCounter:
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(encoding_name)
        except Exception:
            logger.warning("tiktoken no disponible; se usa conteo aproximado por caracteres")
            self._encoding = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            # disallowed_special=() hace que tokens especiales escritos por el usuario
            # (p. ej. "<|endoftext|>") se cuenten como texto común en vez de fallar.
            return len(self._encoding.encode(text, disallowed_special=()))
        return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        if self._encoding is None:
            return text[: int(max_tokens * _CHARS_PER_TOKEN)]
        tokens = self._encoding.encode(text, disallowed_special=())
        if len(tokens) <= max_tokens:
            return text
        return self._encoding.decode(tokens[:max_tokens])
