"""División en fragmentos (chunks) autocontenidos para la recuperación.

Reemplaza al splitter recursivo de LangChain: corta por párrafos, después por líneas, oraciones
y palabras, con solapamiento para no partir una idea entre dos fragmentos sin contexto.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from iamia.core.text import stable_hash
from iamia.rag.ingest.sources import FaqEntry

SEPARATORS = ("\n\n", "\n", ". ", "; ", ", ", " ")


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    url: str
    content: str
    question: str
    category: str
    source: str


def _pieces(text: str, separator: str) -> list[str]:
    parts = text.split(separator)
    # El separador queda pegado al final de cada pieza (conserva puntuación y saltos).
    return [part + separator for part in parts[:-1]] + [parts[-1]]


def split_recursive(text: str, chunk_size: int, overlap: int, separators: Sequence[str] = SEPARATORS) -> list[str]:
    if chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("chunk_size debe ser positivo y overlap menor que chunk_size")
    text = text.strip()
    if len(text) <= chunk_size:
        return [text] if text else []
    separator = next((sep for sep in separators if sep in text), None)
    if separator is None:
        step = chunk_size - overlap
        return [text[i : i + chunk_size].strip() for i in range(0, len(text), step) if text[i : i + chunk_size].strip()]
    rest = separators[separators.index(separator) + 1 :]
    chunks: list[str] = []
    current = ""
    for piece in _pieces(text, separator):
        if len(piece) > chunk_size:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(split_recursive(piece, chunk_size, overlap, rest))
            continue
        if len(current) + len(piece) > chunk_size and current.strip():
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            space = tail.find(" ")
            tail = tail[space + 1 :] if space >= 0 else tail
            current = tail if len(tail) + len(piece) <= chunk_size else ""
        current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def build_chunks(entries: Iterable[FaqEntry], chunk_size: int = 1000, chunk_overlap: int = 150) -> list[Chunk]:
    """Cada fragmento repite trámite y pregunta para que se entienda solo al recuperarlo."""
    chunks: list[Chunk] = []
    for entry in entries:
        header = (f"Trámite: {entry.category}\n" if entry.category else "") + f"Pregunta: {entry.question}\n"
        entry_id = stable_hash(entry.url, entry.question, entry.answer)[:20]
        for index, piece in enumerate(split_recursive(entry.answer, chunk_size, chunk_overlap)):
            chunks.append(
                Chunk(
                    id=f"{entry_id}-{index}",
                    title=(entry.category or entry.question)[:300],
                    url=entry.url,
                    content=f"{header}Respuesta: {piece}",
                    question=entry.question[:500],
                    category=entry.category[:200],
                    source=entry.source,
                )
            )
    return chunks


def digest(chunks: Iterable[Chunk]) -> str:
    """Huella del contenido: si no cambió, no hace falta reindexar."""
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk.id.encode())
        hasher.update(chunk.content.encode())
        hasher.update(chunk.url.encode())
    return hasher.hexdigest()
